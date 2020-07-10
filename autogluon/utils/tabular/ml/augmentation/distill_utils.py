import copy, time, traceback, logging, os, gc
import numpy as np
import pandas as pd
from sklearn.neighbors import NearestNeighbors

from ..constants import BINARY, MULTICLASS, REGRESSION, SOFTCLASS
from ..models.tabular_nn.tabular_nn_model import TabularNeuralNetModel
from ...metrics import mean_squared_error

logger = logging.getLogger(__name__)

def format_distillation_labels(y_train, y_test, problem_type, num_classes=None, eps_labelsmooth=0.01):
    """ Transforms train/test label objects to the correct type for distillation.
        eps_labelsmooth : truncates labels to [EPS, 1-EPS], eg. when converting binary problems -> regression
    """
    if problem_type == MULTICLASS:
        y_train_int = y_train.to_numpy()
        y_train = np.zeros((y_train_int.size, num_classes))
        y_train[np.arange(y_train_int.size),y_train_int] = 1
        y_train = pd.DataFrame(y_train)
        y_test_int = y_test.to_numpy()
        y_test = np.zeros((y_test_int.size, num_classes))
        y_test[np.arange(y_test_int.size),y_test_int] = 1
        y_test = pd.DataFrame(y_test)
    elif problem_type == BINARY:
        min_pred = 0.0
        max_pred = 1.0
        y_train = eps_labelsmooth + ((1-2*eps_labelsmooth)/(max_pred-min_pred)) * (y_train - min_pred)
        y_test = eps_labelsmooth + ((1-2*eps_labelsmooth)/(max_pred-min_pred)) * (y_test - min_pred)

    return (y_train, y_test)

def augment_data(X_train, feature_types_metadata, augmentation_data=None, augment_method='spunge', augment_args={}):
    """ augment_method options: ['spunge', 'munge']
    """
    if augmentation_data is not None:
        X_aug = augmentation_data
    else:
        if 'num_augmented_samples' not in augment_args:
            if 'max_size' not in augment_args:
                augment_args['max_size'] = np.inf
            augment_args['num_augmented_samples'] = int(min(augment_args['max_size'], augment_args['size_factor']*len(X_train)))

        if augment_method == 'spunge':
            X_aug = spunge_augment(X_train, feature_types_metadata, **augment_args)
        elif augment_method == 'munge':
            X_aug = munge_augment(X_train, feature_types_metadata, **augment_args)
        else:
            raise ValueError(f"unknown augment_method: {augment_method}")

    return postprocess_augmented(X_aug, X_train)

def postprocess_augmented(X_aug, X):
    X_aug = pd.concat([X, X_aug])
    # X_aug.drop_duplicates(keep='first', inplace=True)  # remove duplicate points including those in original training data already.
    # TODO: dropping duplicates is much more efficient, but may skew distribution for entirely-categorical data with few categories.
    X_aug = X_aug.tail(len(X_aug)-len(X))
    logger.log(15, f"Augmented training dataset with {len(X_aug)} extra datapoints")
    return X_aug.reset_index(drop=True, inplace=False)

# To grid-search {frac_perturb,continuous_feature_noise}: call spunge_augment() many times and track validation score in Trainer.
def spunge_augment(X, feature_types_metadata, num_augmented_samples = 10000, frac_perturb = 0.1,
                   continuous_feature_noise = 0.1, **kwargs):
    """ Generates synthetic datapoints for learning to mimic teacher model in distillation
        via simplified version of MUNGE strategy (that does not require near-neighbor search).

        Args:
            num_augmented_samples: number of additional augmented data points to return
            frac_perturb: fraction of features/examples that are perturbed during augmentation. Set near 0 to ensure augmented sample distribution remains closer to real data.
            continuous_feature_noise: we noise numeric features by this factor times their std-dev. Set near 0 to ensure augmented sample distribution remains closer to real data.
    """
    if frac_perturb > 1.0:
        raise ValueError("frac_perturb must be <= 1")
    logger.log(20, f"SPUNGE: Augmenting training data with {num_augmented_samples} synthetic samples for distillation...")
    num_feature_perturb = max(1, int(frac_perturb*len(X.columns)))
    X_aug = pd.concat([X.iloc[[0]].copy()]*num_augmented_samples)
    X_aug.reset_index(drop=True, inplace=True)
    continuous_types = ['float','int', 'datetime']
    continuous_featnames = [] # these features will have shuffled values with added noise
    for contype in continuous_types:
        if contype in feature_types_metadata:
            continuous_featnames += feature_types_metadata[contype]

    for i in range(num_augmented_samples): # hot-deck sample some features per datapoint
        og_ind = i % len(X)
        augdata_i = X.iloc[og_ind].copy()
        num_feature_perturb_i = np.random.choice(range(1,num_feature_perturb+1))  # randomly sample number of features to perturb
        cols_toperturb = np.random.choice(list(X.columns), size=num_feature_perturb_i, replace=False)
        for feature in cols_toperturb:
            feature_data = X[feature]
            augdata_i[feature] = feature_data.sample(n=1).values[0]
        X_aug.iloc[i] = augdata_i

    for feature in X.columns:
        if feature in continuous_featnames:
            feature_data = X[feature]
            aug_data = X_aug[feature]
            noise = np.random.normal(scale=np.nanstd(feature_data)*continuous_feature_noise, size=num_augmented_samples)
            mask = np.random.binomial(n=1, p=frac_perturb, size=num_augmented_samples)
            aug_data = aug_data + noise*mask
            X_aug[feature] = pd.Series(aug_data, index=X_aug.index)

    return X_aug


# Example: z = munge_augment(train_data[:100], trainer.feature_types_metadata, num_augmented_samples=25, s= 0.1, perturb_prob=0.9)
# To grid-search {p,s}: call munge_augment() many times and track validation score in Trainer.
def munge_augment(X, feature_types_metadata, num_augmented_samples = 10000, perturb_prob = 0.5, s = 1.0, **kwargs):
    """ Use MUNGE to generate synthetic datapoints for learning to mimic teacher model in distillation.
        Args:
            num_augmented_samples: number of additional augmented data points to return
            perturb_prob: probability of perturbing each feature during augmentation. Set near 0 to ensure augmented sample distribution remains closer to real data.
            s: We noise numeric features by their std-dev divided by this factor (inverse of continuous_feature_noise). Set large to ensure augmented sample distribution remains closer to real data.
    """
    nn_dummy = TabularNeuralNetModel( path='nn_dummy', name='nn_dummy', problem_type=REGRESSION, objective_func=mean_squared_error,
                    hyperparameters={'num_dataloading_workers':0,'proc.embed_min_categories':np.inf}, features = list(X.columns))
    nn_dummy.feature_types_metadata = feature_types_metadata
    processed_data = nn_dummy.process_train_data(df=nn_dummy.preprocess(X), labels=pd.Series([1]*len(X)), batch_size=nn_dummy.params['batch_size'],
                        num_dataloading_workers=0, impute_strategy=nn_dummy.params['proc.impute_strategy'],
                        max_category_levels=nn_dummy.params['proc.max_category_levels'], skew_threshold=nn_dummy.params['proc.skew_threshold'],
                        embed_min_categories=nn_dummy.params['proc.embed_min_categories'], use_ngram_features=nn_dummy.params['use_ngram_features'])
    X_vector = processed_data.dataset._data[processed_data.vectordata_index].asnumpy()
    processed_data = None
    nn_dummy = None
    gc.collect()

    neighbor_finder = NearestNeighbors(n_neighbors=2)
    neighbor_finder.fit(X_vector)
    neigh_dist, neigh_ind = neighbor_finder.kneighbors(X_vector)
    neigh_ind = neigh_ind[:,1]  # contains indices of nearest neighbors
    neigh_dist = None
    # neigh_dist = neigh_dist[:,1]  # contains distances to nearest neighbors
    neighbor_finder = None
    gc.collect()

    if perturb_prob > 1.0:
        raise ValueError("frac_perturb must be <= 1")
    logger.log(20, f"MUNGE: Augmenting training data with {num_augmented_samples} synthetic samples for distillation...")
    X = X.copy()
    X_aug = pd.concat([X.iloc[[0]].copy()]*num_augmented_samples)
    X_aug.reset_index(drop=True, inplace=True)
    continuous_types = ['float','int', 'datetime']
    continuous_featnames = [] # these features will have shuffled values with added noise
    for contype in continuous_types:
        if contype in feature_types_metadata:
            continuous_featnames += feature_types_metadata[contype]
    for col in continuous_featnames:
        X_aug[col] = X_aug[col].astype(float)
        X[col] = X[col].astype(float)
    """
    column_list = X.columns.tolist()
    numer_colinds = [j for j in range(len(column_list)) if column_list[j] in continuous_featnames]
    categ_colinds = [j for j in range(len(column_list)) if column_list[j] not in continuous_featnames]
    numer_std_devs = [np.std(X.iloc[:,j]) for j in numer_colinds]  # list whose jth element = std dev of the jth numerical feature
    """
    for i in range(num_augmented_samples):
        og_ind = i % len(X)
        augdata_i = X.iloc[og_ind].copy()
        neighbor_i = X.iloc[neigh_ind[og_ind]].copy()
        # dist_i = neigh_dist[og_ind]
        cols_toperturb = np.random.choice(list(X.columns), size=np.random.binomial(X.shape[1], p=perturb_prob, size=1)[0], replace=False)
        for col in cols_toperturb:
            new_val = neighbor_i[col]
            if col in continuous_featnames:
                new_val += np.random.normal(scale=np.abs(augdata_i[col]-new_val)/s)
            augdata_i[col] = new_val
        X_aug.iloc[i] = augdata_i

    return X_aug

def nearest_neighbor(numer_i, categ_i, numer_candidates, categ_candidates):
    """ Returns tuple (index, dist) of nearest neighbor point in the list of candidates (pd.DataFrame) to query point i (pd.Series).
        Uses Euclidean distance for numerical features, Hamming for categorical features.
    """
    from sklearn.metrics.pairwise import paired_euclidean_distances
    dists = paired_euclidean_distances(numer_i.to_numpy(), numer_candidates.to_numpy())
    return (index, distance)
