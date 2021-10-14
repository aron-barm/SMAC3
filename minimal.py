import numpy as np

from sklearn.ensemble import RandomForestClassifier
from ConfigSpace import ConfigurationSpace
from ConfigSpace.hyperparameters import UniformIntegerHyperparameter
from smac.facade.smac_bb_facade import SMAC4BB
from smac.scenario.scenario import Scenario


X_train, y_train = np.random.randint(2, size=(20, 2)), np.random.randint(2, size=20)
X_val, y_val = np.random.randint(2, size=(5, 2)), np.random.randint(2, size=5)


def train_random_forest(config):
    """ 
    Trains a random forest on the given hyperparameters, defined by config, and returns the accuracy
    on the validation data.

    Input:
        config (Configuration): Configuration object derived from ConfigurationSpace.

    Return:
        cost (float): Performance measure on the validation data.
    """
    model = RandomForestClassifier(max_depth=config["depth"])
    model.fit(X_train, y_train)

    # define the evaluation metric as return
    return 1 - model.score(X_val, y_val)


if __name__ == "__main__":
    # Define your hyperparameters
    configspace = ConfigurationSpace()
    configspace.add_hyperparameter(UniformIntegerHyperparameter("depth", 2, 100))

    # Provide meta data for the optimization
    scenario = Scenario({
        "run_obj": "quality",  # Optimize quality (alternatively runtime)
        "runcount-limit": 10,  # Max number of function evaluations (the more the better)
        "cs": configspace,
    })

    smac = SMAC4BB(scenario=scenario, tae_runner=train_random_forest)
    best_found_config = smac.optimize()
