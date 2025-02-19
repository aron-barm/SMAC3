from typing import Dict, Iterator, List, Optional, Tuple, Type, Union

import copy
from itertools import chain

import numpy as np
from ConfigSpace.hyperparameters import NumericalHyperparameter

from smac.configspace import Configuration
from smac.epm.base_epm import BaseEPM
from smac.epm.gaussian_process.augmented import GloballyAugmentedLocalGaussianProcess
from smac.epm.random_forest.rf_with_instances import RandomForestWithInstances
from smac.epm.utils import get_types
from smac.optimizer.acquisition import EI, TS, AbstractAcquisitionFunction
from smac.optimizer.acquisition.maximizer import AcquisitionFunctionMaximizer
from smac.optimizer.configuration_chooser.epm_chooser import EPMChooser
from smac.optimizer.configuration_chooser.random_chooser import (
    ChooserNoCoolDown,
    RandomChooser,
)
from smac.optimizer.subspaces.boing_subspace import BOinGSubspace
from smac.optimizer.subspaces.turbo_subspace import TuRBOSubSpace
from smac.runhistory.runhistory import RunHistory
from smac.runhistory.runhistory2epm_boing import RunHistory2EPM4CostWithRaw
from smac.scenario.scenario import Scenario
from smac.stats.stats import Stats
from smac.utils.constants import MAXINT


class BOinGChooser(EPMChooser):
    """
    Interface to train the EPM and generate next configurations with both global and local models.

    Parameters
    ----------
    runhistory2epm: RunHistory2EPM4CostWithRaw,
        a transformer to transform rh to vectors, different from the rh2epm used in vanilla EPMChooser, this rh2epm
        object needs to provide the raw values for optimizer in different stages
    model: smac.epm.rf_with_instances.RandomForestWithInstances
        empirical performance model (right now, we support only RandomForestWithInstances) as a global model
    acq_optimizer: smac.optimizer.ei_optimization.AcquisitionFunctionMaximizer
        Optimizer of acquisition function of global models
    model_local: BaseEPM,
        local empirical performance model, used in subspace
    model_local_kwargs: Optional[Dict] = None,
        parameters for initializing a local model
    acquisition_func_local: AbstractAcquisitionFunction,
        local acquisition function,  used in subspace
    acquisition_func_local_kwargs: Optional[Dict] = None,
        parameters for initializing a local acquisition function optimizer
    acq_optimizer_local: Optional[AcquisitionFunctionMaximizer] = None,
        Optimizer of acquisition function of local models
    acq_optimizer_local_kwargs: typing: Optional[Dict] = None,
        parameters for the optimizer of acquisition function of local models
    max_configs_local_fracs : float
        The maximal number of fractions of samples to be included in the subspace. If the number of samples in the
        subspace is greater than this value and n_min_config_inner, the subspace will be cropped to fit the requirement
    min_configs_local: int,
        Minimum number of samples included in the inner loop model
    do_switching: bool
       if we want to switch between turbo and boing or do a pure BOinG search
    turbo_kwargs: Optional[Dict] = None
       parameters for building a turbo optimizer
    """

    def __init__(
        self,
        scenario: Scenario,
        stats: Stats,
        runhistory: RunHistory,
        runhistory2epm: RunHistory2EPM4CostWithRaw,
        model: RandomForestWithInstances,
        acq_optimizer: AcquisitionFunctionMaximizer,
        acquisition_func: AbstractAcquisitionFunction,
        rng: np.random.RandomState,
        restore_incumbent: Configuration = None,
        random_configuration_chooser: RandomChooser = ChooserNoCoolDown(2.0),
        predict_x_best: bool = True,
        min_samples_model: int = 1,
        model_local: Union[BaseEPM, Type[BaseEPM]] = GloballyAugmentedLocalGaussianProcess,
        acquisition_func_local: Union[AbstractAcquisitionFunction, Type[AbstractAcquisitionFunction]] = EI,
        model_local_kwargs: Optional[Dict] = None,
        acquisition_func_local_kwargs: Optional[Dict] = None,
        acq_optimizer_local: Optional[AcquisitionFunctionMaximizer] = None,
        acq_optimizer_local_kwargs: Optional[Dict] = None,
        max_configs_local_fracs: float = 0.5,
        min_configs_local: Optional[int] = None,
        do_switching: bool = False,
        turbo_kwargs: Optional[Dict] = None,
    ):
        # initialize the original EPM_Chooser
        super(BOinGChooser, self).__init__(
            scenario=scenario,
            stats=stats,
            runhistory=runhistory,
            runhistory2epm=runhistory2epm,
            model=model,
            acq_optimizer=acq_optimizer,
            acquisition_func=acquisition_func,
            rng=rng,
            restore_incumbent=restore_incumbent,
            random_configuration_chooser=random_configuration_chooser,
            predict_x_best=predict_x_best,
            min_samples_model=min_samples_model,
        )
        if not isinstance(self.model, RandomForestWithInstances):
            raise ValueError("BOinG only supports RandomForestWithInstances as its global optimizer")
        if not isinstance(self.rh2EPM, RunHistory2EPM4CostWithRaw):
            raise ValueError("BOinG only supports RunHistory2EPM4CostWithRaw as its rh transformer")

        cs = self.scenario.cs  # type: ignore

        self.subspace_info = {
            "model_local": model_local,
            "model_local_kwargs": model_local_kwargs,
            "acq_func_local": acquisition_func_local,
            "acq_func_local_kwargs": acquisition_func_local_kwargs,
            "acq_optimizer_local": acq_optimizer_local,
            "acq_optimizer_local_kwargs": acq_optimizer_local_kwargs,
        }

        self.max_configs_local_fracs = max_configs_local_fracs
        self.min_configs_local = (
            min_configs_local if min_configs_local is not None else 5 * len(cs.get_hyperparameters())
        )

        types, bounds = get_types(cs, instance_features=None)

        self.types = types
        self.bounds = bounds
        self.cat_dims = np.where(np.array(types) != 0)[0]
        self.cont_dims = np.where(np.array(types) == 0)[0]
        self.config_space = cs

        self.frac_to_start_bi = 0.8
        self.split_count = np.zeros(len(types))
        self.do_switching = do_switching
        self.random_search_upper_log = 1

        self.optimal_value = np.inf
        self.optimal_config = None

        self.ss_threshold = 0.1 ** len(cs.get_hyperparameters())
        if self.do_switching:
            # If we want to switch between BOinG and TurBO
            self.run_TuRBO = False
            self.failcount_BOinG = 0
            self.failcount_TurBO = 0

            turbo_model = copy.deepcopy(model_local)
            turbo_acq = TS
            turbo_opt_kwargs = dict(
                config_space=cs,
                bounds=bounds,
                hps_types=types,
                model_local=turbo_model,
                model_local_kwargs=copy.deepcopy(model_local_kwargs),
                acq_func_local=turbo_acq,
                rng=rng,
                length_min=2e-4,
            )
            self.turbo_kwargs = turbo_opt_kwargs
            if turbo_kwargs is not None:
                turbo_opt_kwargs.update(turbo_kwargs)
            self.turbo_optimizer = TuRBOSubSpace(**turbo_opt_kwargs)

    def restart_TuRBOinG(self, X: np.ndarray, Y: np.ndarray, Y_raw: np.ndarray, train_model: bool = False) -> None:
        """
        Restart a new TurBO Optimizer, the bounds of the TurBO Optimizer is determined by a RF, we randomly sample 20
        points and extract subspaces that contain at least self.min_configs_local points, and we select the subspace
        with the largest volume to construct a turbo optimizer
        Parameters
        ----------
        X: np.ndarray (N, D)
            previous evaluated configurations
        Y: np.ndarray (N,)
            performances of previous evaluated configurations (transformed by rh2epm transformer)
        Y_raw: np.ndarray (N,)
            performances of previous evaluated configurations (raw values, not transformed)
        train_model: bool
            if we retrain the model with the given X and Y
        """
        if train_model:
            self.model.train(X, Y)
        num_samples = 20
        union_ss = []
        union_indices = []
        rand_samples = self.config_space.sample_configuration(num_samples)
        for sample in rand_samples:
            sample_array = sample.get_array()
            union_bounds_cont, _, ss_data_indices = subspace_extraction(
                X=X,
                challenger=sample_array,
                model=self.model,
                num_min=self.min_configs_local,
                num_max=MAXINT,
                bounds=self.bounds,
                cont_dims=self.cont_dims,
                cat_dims=self.cat_dims,
            )
            union_ss.append(union_bounds_cont)
            union_indices.append(ss_data_indices)
        union_ss = np.asarray(union_ss)
        volume_ss = np.product(union_ss[:, :, 1] - union_ss[:, :, 0], axis=1)  # type: ignore
        ss_idx = np.argmax(volume_ss)
        ss_turbo = union_ss[ss_idx]
        ss_data_indices = union_indices[ss_idx]

        # we only consider numerical(continuous) hyperparameters here
        self.turbo_optimizer = TuRBOSubSpace(
            **self.turbo_kwargs,  # type: ignore
            bounds_ss_cont=ss_turbo,  # type: ignore
            initial_data=(X[ss_data_indices], Y_raw[ss_data_indices]),  # type: ignore
        )
        self.turbo_optimizer.add_new_observations(X[ss_data_indices], Y_raw[ss_data_indices])

    def choose_next(self, incumbent_value: float = None) -> Iterator[Configuration]:
        """
        Choose next candidate solution with Bayesian optimization. We use TurBO optimizer or BOinG to suggest
         the next configuration.
        If we switch local model between TurBO and BOinG, we gradually increase the probability to switch to another
        optimizer if we cannot make further process. (Or if TurBO find a new incumbent, we will switch to BOinG to do
        further exploitation)

        Parameters
        ----------
        incumbent_value: float
            Cost value of incumbent configuration (required for acquisition function);
            If not given, it will be inferred from runhistory or predicted;
            if not given and runhistory is empty, it will raise a ValueError.

        Returns
        -------
        Iterator
        """
        # we also need the untransformed raw y values to used for local models
        X, Y, Y_raw, X_configurations = self._collect_all_data_to_train_model()
        if self.do_switching:
            if self.run_TuRBO:
                X, Y, Y_raw, X_configurations = self._collect_all_data_to_train_model()

                num_new_bservations = 1  # here we only consider batch_size ==1

                new_observations = Y_raw[-num_new_bservations:]

                # give new suggestions from initialized values in TurBO
                if len(self.turbo_optimizer.init_configs) > 0:
                    self.turbo_optimizer.add_new_observations(X[-num_new_bservations:], Y_raw[-num_new_bservations:])
                    return self.turbo_optimizer.generate_challengers()

                self.turbo_optimizer.adjust_length(new_observations)

                # if we need to restart TurBO, we first check if we want to switch to BOinG
                if self.turbo_optimizer.length < self.turbo_optimizer.length_min:
                    optimal_turbo = np.min(self.turbo_optimizer.ss_y)

                    self.logger.debug(f"Best Found value by TuRBO: {optimal_turbo}")

                    increment = optimal_turbo - self.optimal_value

                    if increment < 0:
                        min_idx = np.argmin(Y_raw)
                        self.optimal_value = Y_raw[min_idx].item()
                        # compute the distance between the previous incumbent and new incumbent
                        cfg_diff = X[min_idx] - self.optimal_config
                        self.optimal_config = X[min_idx]
                        # we avoid sticking to a local minimum too often, e.g. either we have a relative much better
                        # configuration or the new configuration is a little bit far away from the current incumbent
                        if (
                            increment < -1e-3 * np.abs(self.optimal_value)
                            or np.abs(np.product(cfg_diff)) >= self.ss_threshold
                        ):
                            self.failcount_TurBO -= 1
                            # switch to BOinG as TurBO found a better model and we could do exploration
                            # also we halve the failcount of BOinG to avoid switching to TurBO too frequently
                            self.failcount_BOinG = self.failcount_BOinG // 2
                            self.run_TuRBO = False
                            self.logger.debug("Optimizer switches to BOinG!")

                    else:
                        self.failcount_TurBO += 1

                    # The probability is a linear curve.
                    prob_to_BOinG = 0.1 * self.failcount_TurBO
                    self.logger.debug(f"failure_count TuRBO :{self.failcount_TurBO}")
                    rand_value = self.rng.random()

                    if rand_value < prob_to_BOinG:
                        self.failcount_BOinG = self.failcount_BOinG // 2
                        self.run_TuRBO = False
                        self.logger.debug("Optimizer switches to BOinG!")
                    else:
                        self.restart_TuRBOinG(X=X, Y=Y, Y_raw=Y_raw, train_model=True)
                        return self.turbo_optimizer.generate_challengers()

                self.turbo_optimizer.add_new_observations(X[-num_new_bservations:], Y_raw[-num_new_bservations:])

                return self.turbo_optimizer.generate_challengers()

        if X.shape[0] == 0:
            # Only return a single point to avoid an overly high number of
            # random search iterations
            return self._random_search.maximize(runhistory=self.runhistory, stats=self.stats, num_points=1)
        # if the number of points is not big enough, we simply build one subspace (the raw configuration space) and
        # the local model becomes global model
        if X.shape[0] < (self.min_configs_local / self.frac_to_start_bi):
            if len(self.config_space.get_conditions()) == 0:
                self.model.train(X, Y)
                cs = self.scenario.cs  # type: ignore
                ss = BOinGSubspace(
                    config_space=cs,
                    bounds=self.bounds,
                    hps_types=self.types,
                    rng=self.rng,
                    initial_data=(X, Y_raw),
                    incumbent_array=None,
                    model_local=self.subspace_info["model_local"],  # type: ignore
                    model_local_kwargs=self.subspace_info["model_local_kwargs"],  # type: ignore
                    acq_func_local=self.subspace_info["acq_func_local"],  # type: ignore
                    acq_func_local_kwargs=self.subspace_info["acq_func_local_kwargs"],  # type: ignore
                    acq_optimizer_local=self.acq_optimizer,
                )
                return ss.generate_challengers()

        # train the outer model
        self.model.train(X, Y)

        if incumbent_value is not None:
            best_observation = incumbent_value
            x_best_array = None  # type: Optional[np.ndarray]
        else:
            if self.runhistory.empty():
                raise ValueError("Runhistory is empty and the cost value of " "the incumbent is unknown.")
            x_best_array, best_observation = self._get_x_best(self.predict_x_best, X_configurations)

        self.acquisition_func.update(
            model=self.model,
            eta=best_observation,
            incumbent_array=x_best_array,
            num_data=len(self._get_evaluated_configs()),
            X=X_configurations,
        )

        if self.do_switching:
            # check if we need to switch to turbo
            # same as above
            self.failcount_BOinG += 1
            increment = Y_raw[-1].item() - self.optimal_value
            if increment < 0:
                if self.optimal_config is not None:
                    cfg_diff = X[-1] - self.optimal_config
                    if (
                        increment < -1e-2 * np.abs(self.optimal_value)
                        or np.abs(np.product(cfg_diff)) >= self.ss_threshold
                    ):
                        self.failcount_BOinG -= X.shape[-1]
                    self.optimal_value = Y_raw[-1].item()
                    self.optimal_config = X[-1]
                else:
                    # restart
                    idx_min = np.argmin(Y_raw)
                    self.logger.debug("Better value found by BOinG, continue BOinG")
                    self.optimal_value = Y_raw[idx_min].item()
                    self.optimal_config = X[idx_min]
                    self.failcount_BOinG = 0

            # similar to TurBO, we do a judgement every n_dimension times
            amplify_param = self.failcount_BOinG // (X.shape[-1] * 1)

            if self.failcount_BOinG % (X.shape[-1] * 1) == 0:
                prob_to_TurBO = 0.1 * amplify_param
                rand_value = self.rng.random()

                if rand_value < prob_to_TurBO:
                    self.run_TuRBO = True
                    self.logger.debug("Switch To TuRBO")
                    self.failcount_TurBO = self.failcount_TurBO // 2
                    self.restart_TuRBOinG(X=X, Y=Y, Y_raw=Y_raw, train_model=False)

        challengers_global = self.acq_optimizer.maximize(
            runhistory=self.runhistory,
            stats=self.stats,
            num_points=self.scenario.acq_opt_challengers,  # type: ignore[attr-defined] # noqa F821
            random_configuration_chooser=self.random_configuration_chooser,
        )

        if (
            X.shape[0] < (self.min_configs_local / self.frac_to_start_bi)
            and len(self.config_space.get_conditions()) == 0
        ):
            return challengers_global

        cfg_challenger_global_first = next(challengers_global)
        array_challenger_global_first = cfg_challenger_global_first.get_array()  # type: np.ndarray

        num_max_configs = int(X.shape[0] * self.max_configs_local_fracs)

        # to avoid the case that num_max_configs is only a little larger than self.min_configs_local
        num_max = MAXINT if num_max_configs <= 2 * self.min_configs_local else num_max_configs

        if len(self.config_space.get_conditions()) > 0:
            challanger_activate_hps = np.isfinite(array_challenger_global_first).astype(int)
            rh_activate_hps = np.isfinite(X).astype(int)
            indices_X_in_same_hierarchy = np.all((challanger_activate_hps - rh_activate_hps) == 0, axis=1)
            num_indices_X_in_same_hierarchy = sum(indices_X_in_same_hierarchy)

            if num_indices_X_in_same_hierarchy == 0:
                return chain([cfg_challenger_global_first], challengers_global)

            activate_dims = []
            hps = self.config_space.get_hyperparameters()
            for idx_hp in np.where(challanger_activate_hps > 0)[0]:
                if isinstance(hps[idx_hp], NumericalHyperparameter):
                    activate_dims.append(idx_hp)
                else:
                    indices_X_in_same_hierarchy = indices_X_in_same_hierarchy & (
                        X[:, idx_hp] == array_challenger_global_first[idx_hp]
                    )
            num_indices_X_in_same_hierarchy = sum(indices_X_in_same_hierarchy)

            X = X[indices_X_in_same_hierarchy]
            Y_raw = Y_raw[indices_X_in_same_hierarchy]

            if len(activate_dims) == 0 or num_indices_X_in_same_hierarchy <= max(5, len(activate_dims)):
                return chain([cfg_challenger_global_first], challengers_global)
            n_min_configs_inner = self.min_configs_local // len(hps) * len(activate_dims)
        else:
            n_min_configs_inner = self.min_configs_local
            activate_dims = np.arange(len(self.config_space.get_hyperparameters()))

        bounds_ss_cont, bounds_ss_cat, ss_data_indices = subspace_extraction(
            X=X,
            challenger=array_challenger_global_first,
            model=self.model,
            num_min=n_min_configs_inner,
            num_max=num_max,
            bounds=self.bounds,
            cont_dims=self.cont_dims,
            cat_dims=self.cat_dims,
        )

        self.logger.debug("contained {0} data of {1}".format(sum(ss_data_indices), Y_raw.size))

        ss = BOinGSubspace(
            config_space=self.scenario.cs,  # type: ignore
            bounds=self.bounds,
            hps_types=self.types,
            bounds_ss_cont=bounds_ss_cont,  # type: ignore[arg-type]
            bounds_ss_cat=bounds_ss_cat,
            rng=self.rng,
            initial_data=(X, Y_raw),
            incumbent_array=array_challenger_global_first,  # type: ignore[arg-type]
            activate_dims=activate_dims,
            **self.subspace_info,  # type: ignore[arg-type]
        )
        return ss.generate_challengers()

    def _collect_all_data_to_train_model(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Similar to the implementaiton of EPMChooser, however, we also return the raw values here."""
        # if we use a float value as a budget, we want to train the model only on the highest budget
        available_budgets = []
        for run_key in self.runhistory.data.keys():
            available_budgets.append(run_key.budget)

        # Sort available budgets from highest to lowest budget
        available_budgets = sorted(list(set(available_budgets)), reverse=True)

        # Get #points per budget and if there are enough samples, then build a model
        for b in available_budgets:
            X, Y, Y_raw = self.rh2EPM.transform_with_raw(  # type: ignore[attr-defined]
                self.runhistory,
                budget_subset=[
                    b,
                ],
            )  # type: ignore
            if X.shape[0] >= self.min_samples_model:
                self.currently_considered_budgets = [
                    b,
                ]
                configs_array = self.rh2EPM.get_configurations(
                    self.runhistory, budget_subset=self.currently_considered_budgets
                )
                return X, Y, Y_raw, configs_array

        return (
            np.empty(shape=[0, 0]),
            np.empty(
                shape=[
                    0,
                ]
            ),
            np.empty(
                shape=[
                    0,
                ]
            ),
            np.empty(shape=[0, 0]),
        )


def subspace_extraction(
    X: np.ndarray,
    challenger: np.ndarray,
    model: RandomForestWithInstances,
    num_min: int,
    num_max: int,
    bounds: Union[np.ndarray, List[Tuple]],
    cat_dims: np.ndarray,
    cont_dims: np.ndarray,
) -> Tuple[np.ndarray, List[Tuple], np.ndarray]:
    """
    Extract a subspace that contains at least num_min points but no more than num_max points

    Parameters
    ----------
    X: np.ndarray (N, D)
        points used to train the model
    challenger: np.ndarray (1, D)
        the challenger where the subspace would grow
    model: RandomForestWithInstances
        a rf model
    num_min: int
        minimal number of points to be included in the subspace
    num_max: int
        maximal number of points to be included in the subspace
    bounds: np.ndarray(D, 2)
        bounds of the entire space, D = D_cat + D_cont
    cat_dims: np.ndarray (D_cat)
        categorical dimensions
    cont_dims: np.ndarray(D_cont)
    continuous dimensions

    Returns
    -------
    union_bounds_cont: np.ndarray(D_cont, 2),
         the continuous bounds of the subregion
    union_bounds_cat, List[Tuple],
        the categorical bounds of the subregion
    in_ss_dims:
        indices of the points that lie inside the subregion
    """
    trees = model.rf.get_all_trees()
    trees = [tree for tree in trees]
    num_trees = len(trees)
    node_indices = [0] * num_trees

    indices_trees = np.arange(num_trees)
    np.random.shuffle(indices_trees)
    ss_indices = np.full(X.shape[0], True)  # type: np.ndarray

    stop_update = [False] * num_trees

    ss_bounds = np.array(bounds)

    cont_dims = np.array(cont_dims)
    cat_dims = np.array(cat_dims)

    if len(cat_dims) == 0:
        ss_bounds_cat = [()]
    else:
        ss_bounds_cat = [() for _ in range(len(cat_dims))]
        for i, cat_dim in enumerate(cat_dims):
            ss_bounds_cat[i] = np.arange(ss_bounds[cat_dim][0])

    if len(cont_dims) == 0:
        ss_bounds_cont = np.array([])  # type: np.ndarray
    else:
        ss_bounds_cont = ss_bounds[cont_dims]

    def traverse_forest(check_num_min: bool = True) -> None:
        nonlocal ss_indices
        np.random.shuffle(indices_trees)
        for i in indices_trees:
            if stop_update[i]:
                continue
            tree = trees[int(i)]
            node_idx = node_indices[i]
            node = tree.get_node(node_idx)

            if node.is_a_leaf():
                stop_update[i] = True
                continue

            feature_idx = node.get_feature_index()
            cont_feature_idx = np.where(feature_idx == cont_dims)[0]
            if cont_feature_idx.size == 0:
                # This node split the subspace w.r.t. the categorical hyperparameters
                cat_feature_idx = np.where(feature_idx == cat_dims)[0][0]
                split_value = node.get_cat_split()
                intersect = np.intersect1d(ss_bounds_cat[cat_feature_idx], split_value, assume_unique=True)

                if len(intersect) == len(ss_bounds_cat[cat_feature_idx]):
                    # will fall into the left child
                    temp_child_idx = 0
                    node_indices[i] = node.get_child_index(temp_child_idx)
                elif len(intersect) == 0:
                    # will fall into the left child
                    temp_child_idx = 1
                    node_indices[i] = node.get_child_index(temp_child_idx)
                else:
                    if challenger[feature_idx] in intersect:
                        temp_child_idx = 0
                        temp_node_indices = ss_indices & np.in1d(X[:, feature_idx], split_value)
                        temp_bound_ss = intersect
                    else:
                        temp_child_idx = 1
                        temp_node_indices = ss_indices & np.in1d(X[:, feature_idx], split_value, invert=True)
                        temp_bound_ss = np.setdiff1d(ss_bounds_cat[cat_feature_idx], split_value)
                    if sum(temp_node_indices) > num_min:
                        # number of points inside subspace is still greater than num_min, we could go deeper
                        ss_bounds_cat[cat_feature_idx] = temp_bound_ss
                        ss_indices = temp_node_indices
                        node_indices[i] = node.get_child_index(temp_child_idx)
                    else:
                        if check_num_min:
                            stop_update[i] = True
                        else:
                            # if we don't check the num_min, we will stay go deeper into the child nodes without
                            # splitting the subspace
                            node_indices[i] = node.get_child_index(temp_child_idx)
            else:
                # This node split the subspace w.r.t. the continuous hyperparameters
                split_value = node.get_num_split_value()
                cont_feature_idx = cont_feature_idx.item()
                if ss_bounds_cont[cont_feature_idx][0] <= split_value <= ss_bounds_cont[cont_feature_idx][1]:
                    # the subspace can be further split
                    if challenger[feature_idx] >= split_value:
                        temp_bound_ss = np.array([split_value, ss_bounds_cont[cont_feature_idx][1]])
                        temp_node_indices = ss_indices & (X[:, feature_idx] >= split_value)
                        temp_child_idx = 1
                    else:
                        temp_bound_ss = np.array([ss_bounds_cont[cont_feature_idx][0], split_value])
                        temp_node_indices = ss_indices & (X[:, feature_idx] <= split_value)
                        temp_child_idx = 0
                    if sum(temp_node_indices) > num_min:
                        # number of points inside subspace is still greater than num_min
                        ss_bounds_cont[cont_feature_idx] = temp_bound_ss
                        ss_indices = temp_node_indices
                        node_indices[i] = node.get_child_index(temp_child_idx)
                    else:
                        if check_num_min:
                            stop_update[i] = True
                        else:
                            node_indices[i] = node.get_child_index(temp_child_idx)
                else:
                    temp_child_idx = 1 if challenger[feature_idx] >= split_value else 0
                    node_indices[i] = node.get_child_index(temp_child_idx)

    while sum(stop_update) < num_trees:
        traverse_forest()

    if sum(ss_indices) > num_max:
        # number of points inside the subregion have a larger value than num_max
        stop_update = [False] * num_trees
        while sum(stop_update) < num_trees:
            traverse_forest(False)

    return ss_bounds_cont, ss_bounds_cat, ss_indices  # type: ignore[return-value]
