import os
import random
import weakref
import atexit
import warnings
import itertools
import math
from abc import ABCMeta, abstractmethod
from copy import deepcopy
import tempfile
import time
import sys
from contextlib import contextmanager
import pathlib
import logging

import tqdm
import numpy as np
import tensorflow as tf
from tensorflow.data import Dataset
from tensorflow.compat.v1 import logging as tf_logging

from sklearn.model_selection import train_test_split
import joblib

from finetune.util import list_transpose
from finetune.encoding.input_encoder import EncodedOutput
from finetune.config import get_config, all_gpus, assert_valid_config, get_default_config
from finetune.saver import Saver, InitializeHook
from finetune.errors import FinetuneError
from finetune.model import get_model_fn, PredictMode

from finetune.util.download import download_data_if_required
from finetune.util.positional_embeddings import embedding_preprocessor
from finetune.util.shapes import shape_list
from finetune.base_models import GPTModel, GPTModelSmall
from finetune.nn.auxiliary import add_context_embed

from finetune.util.in_memory_finetune import make_in_memory_finetune_hooks

LOGGER = logging.getLogger("finetune")


class BaseModel(object, metaclass=ABCMeta):
    """
    A sklearn-style task agnostic base class for finetuning a Transformer language model.
    """
    defaults = dict()

    def __init__(self, **kwargs):
        """
        For a full list of configuration options, see `finetune.config`.

        :param config: A config object generated by `finetune.config.get_config` or None (for default config).
        :param **kwargs: key-value pairs of config items to override.
        """
        weak_self = weakref.ref(self)

        def cleanup():
            strong_self = weak_self()
            if strong_self is not None:
                BaseModel.__del__(strong_self)

        atexit.register(cleanup)
        d = deepcopy(self.defaults)
        d.update(kwargs)
        self.config = get_config(**d)
        self.resolved_gpus = None
        self.validate_config()
        download_data_if_required(self.config.base_model)
        self.input_pipeline = self._get_input_pipeline()
        self._trained = False
        self._initialize()
        if self.config.debugging_logs:
            os.environ["TF_CPP_MIN_LOG_LEVEL"] = "0"
            tf_logging.set_verbosity(tf_logging.DEBUG)

    def validate_config(self):
        if (
            self.config.num_layers_trained != self.config.n_layer
            and self.config.train_embeddings
        ):
            raise ValueError(
                "If you are only finetuning a subset of the layers, you cannot finetune embeddings."
            )

    @abstractmethod
    def _get_input_pipeline(self):
        pass

    def _initialize(self):
        # Initializes the non-serialized bits of the class.
        self._set_random_seed(self.config.seed)

        # state for prediction caching
        self._predictions = None
        self._cached_predict = False
        self._closed = False
        self._to_pull = 0

        try:
            self.estimator_dir = os.path.abspath(
                os.path.join(self.config.tensorboard_folder, str(int(time.time())))
            )
            pathlib.Path(self.estimator_dir).mkdir(parents=True, exist_ok=True)
            self._tmp_dir = None
        except (TypeError, IOError):
            # TypeError --> tensorboard_folder is None
            # IOError --> user likely does not have permission to write to the tensorboard_folder directory
            # Both cases we can resolve by
            self._tmp_dir = tempfile.TemporaryDirectory(prefix="Finetune")
            self.estimator_dir = self._tmp_dir.name
            LOGGER.info("Saving tensorboard output to {}".format(self.estimator_dir))

        self.saver = Saver(
            fallback_filename=self.config.base_model_path,
            exclude_matches=None if self.config.save_adam_vars else "Adam",
            variable_transforms=[embedding_preprocessor(self.input_pipeline, self.config)],
            save_dtype=self.config.save_dtype,
            target_model_init_from_base_model=self.config.target_model_init_from_base_model
        )

    @abstractmethod
    def _predict_op(self, logits, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def _predict_proba_op(self, logits, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def _target_model(
        self,
        *,
        config,
        featurizer_state,
        targets,
        n_outputs,
        train=False,
        reuse=None,
        **kwargs
    ):
        # Overridden by subclass to attach a target model onto the shared base featurizer.
        raise NotImplementedError

    def _pre_target_model_hook(self, featurizer_state):
        add_context_embed(featurizer_state)

    def _n_steps(self, n_examples, batch_size, n_gpus):
        steps = int(math.ceil(n_examples / (batch_size * n_gpus)))
        return steps

    def finetune(self, Xs, Y=None, batch_size=None, context=None):
        if (
            not callable(Xs)
            and Y is not None
            and len(Xs) != len(Y)
        ):
            raise FinetuneError(
                "Mismatch between number of examples ({}) and number of targets ({}) provided.".format(
                    len(Xs), len(Y)
                )
            )

        batch_size = batch_size or self.config.batch_size
        val_input_fn, train_input_fn, val_size, val_interval = self.input_pipeline.get_train_input_fns(
            Xs, Y, batch_size=batch_size, context=context
        )

        if self.config.keep_best_model:
            if isinstance(val_size, dict):
                tf.logging.warning("Cannot early stop or keep best model with MTL")
            elif val_size <= 10:
                tf.logging.warning(
                    "Early stopping / keeping best model with a validation size of {} is likely to case undesired results".format(
                        val_size
                    )
                )

        force_build_lm = Y is None
        estimator, hooks = self.get_estimator(force_build_lm=force_build_lm)
        train_hooks = hooks.copy()

        steps_per_epoch = self._n_steps(
            n_examples=self.input_pipeline.dataset_size,
            batch_size=batch_size,
            n_gpus=max(1, len(self.resolved_gpus)),
        )
        num_steps = steps_per_epoch * self.config.n_epochs

        if self.config.tasks is not None:
            # Validation with MTL tasks
            for task in self.config.tasks:
                if val_size[task] > 0:
                    train_hooks.append(
                        tf.estimator.experimental.InMemoryEvaluatorHook(
                            estimator,
                            val_input_fn[task],
                            every_n_iter=val_interval[task],
                            steps=val_size[task] // batch_size,
                            name=task,
                        )
                    )
                    train_hooks.append(
                        tf.estimator.experimental.InMemoryEvaluatorHook(
                            estimator,
                            val_input_fn[task + "_train"],
                            every_n_iter=val_interval[task],
                            steps=val_size[task] // batch_size,
                            name=task + "_train",
                        )
                    )
            early_stopping_interval = sys.maxsize  # turn off early stopping for mtl.
        elif val_size > 0:
            # Validation with all other tasks.
            train_hooks.append(
                tf.estimator.experimental.InMemoryEvaluatorHook(
                    estimator,
                    val_input_fn,
                    every_n_iter=val_interval,
                    steps=math.ceil(val_size / batch_size),
                )
            )
            early_stopping_interval = val_interval
        else:
            early_stopping_interval = sys.maxsize

        train_hooks.append(
            self.saver.get_saver_hook(
                estimator=estimator,
                keep_best_model=self.config.keep_best_model,
                steps_per_epoch=steps_per_epoch,
                early_stopping_steps=self.config.early_stopping_steps,
                eval_frequency=early_stopping_interval,
                cache_weights_to_file=self.config.cache_weights_to_file
            )
        )

        if self.config.in_memory_finetune is not None:
            train_hooks.extend(make_in_memory_finetune_hooks(self, estimator))

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if self.config.prefit_init:
                tf.logging.info("Starting pre-fit initialisation...")
                num_layers_trained = self.config.num_layers_trained
                self.config.num_layers_trained = 0
                estimator.train(train_input_fn, hooks=train_hooks, steps=num_steps)
                self.config.num_layers_trained = num_layers_trained
                self.saver.variables = {
                    k: v
                    for k, v in self.saver.variables.items()
                    if "adam" not in k and "global_step" not in k
                }
                for weight in self.saver.variables:
                    if (
                        weight.startswith("model/target/")
                    ):
                        w = self.saver.variables[weight]
                        if len(w.shape) == 1:
                            continue
                        w_flat = np.reshape(w, [-1, w.shape[-1]])
                        expectation_of_norm = (
                            (self.config.weight_stddev ** 2) * w_flat.shape[0]
                        ) ** 0.5
                        self.saver.variables[weight] = np.reshape(
                            expectation_of_norm
                            * w_flat
                            / np.linalg.norm(w_flat, axis=0),
                            w.shape,
                        )

                tf.logging.info("Finishing pre-fit initialisation...")
            estimator.train(train_input_fn, hooks=train_hooks, steps=num_steps)
        
        self._trained = True

    def _distribute_strategy(self, visible_gpus):
        """
        Select a distribution strategy based on available devices.

        Side effect: sets self.resolved_gpus for future use in computing steps per epoch
        """
        
        if isinstance(visible_gpus, (list, tuple)):
            resolved_gpus = all_gpus(visible_gpus=tuple(visible_gpus))
        else:
            resolved_gpus = all_gpus()

        resolved_gpus_string = ['/gpu:{}'.format(gpu) for gpu in resolved_gpus]
        if len(resolved_gpus_string) == 1:
            distribute_strategy = tf.contrib.distribute.OneDeviceStrategy(resolved_gpus_string[0])
        else:
            if self.config.per_process_gpu_memory_fraction is not None:
                warnings.warn("Setting `per_process_gpu_memory_fraction` is currently unsupported in multi-gpu environments.")

            if isinstance(self.config.distribution_strategy, str):
                if self.config.distribution_strategy.lower() == "mirrored":
                    distribute_strategy = tf.distribute.MirroredStrategy()
                elif self.config.distribution_strategy.lower() == "central_storage":
                    distribute_strategy = tf.distribute.experimental.CentralStorageStrategy(resolved_gpus_string or None)
                else:
                    raise FinetuneError("Distribute strategy {} is not supported, please try \"mirrored\" or \"central_storage\" or an instance of tf.distribute.Strategy")
            elif isinstance(self.config.distribution_strategy, tf.distribute.Strategy):
                distribute_strategy = self.config.distribution_strategy
                    

        self.resolved_gpus = resolved_gpus
        return distribute_strategy

    def _get_estimator_config(self):
        conf = tf.ConfigProto(
            allow_soft_placement=self.config.soft_device_placement,
            log_device_placement=self.config.log_device_placement,
        )
        if self.config.per_process_gpu_memory_fraction is not None:
            conf.gpu_options.per_process_gpu_memory_fraction = (
                self.config.per_process_gpu_memory_fraction
            )
        optimizer_options = conf.graph_options.optimizer_options
        if self.config.xla:                                                     
            optimizer_options.global_jit_level = tf.OptimizerOptions.ON_1 

        distribute_strategy = self._distribute_strategy(self.config.visible_gpus)
        config = tf.estimator.RunConfig(
            tf_random_seed=self.config.seed,
            save_summary_steps=self.config.val_interval,
            save_checkpoints_secs=None,
            save_checkpoints_steps=None,
            # disable auto summaries
            session_config=conf,
            log_step_count_steps=100,
            train_distribute=distribute_strategy,
            keep_checkpoint_max=1,
        )
        return config

    def get_estimator(self, force_build_lm=False, build_explain=False):
        build_lm = force_build_lm or self.config.lm_loss_coef > 0.0
        config = self._get_estimator_config()
        model_fn = get_model_fn(
            target_model_fn=self._target_model,
            pre_target_model_hook=self._pre_target_model_hook,
            predict_op=self._predict_op,
            predict_proba_op=self._predict_proba_op,
            build_target_model=self.input_pipeline.target_dim is not None,
            lm_type=self.config.lm_type if build_lm else None,
            encoder=self.input_pipeline.text_encoder,
            target_dim=self.input_pipeline.target_dim,
            label_encoder=self.input_pipeline.label_encoder,
            build_explain=build_explain,
            n_replicas=max(1, len(self.resolved_gpus))
        )

        hooks = [InitializeHook(self.saver)]
        est = tf.estimator.Estimator(
            model_dir=self.estimator_dir,
            model_fn=model_fn,
            config=config,
            params=self.config,
        )

        return est, hooks

    def close(self):
        self._closed = True

        if self._predictions is not None:

            # force input fn termination
            try:
                for _ in self._predictions:
                    pass
            except AttributeError:
                pass

            self._predictions = None

    def _clear_prediction_queue(self):
        # Flush examples used to pad the last batch
        # of previous call to predict()
        for i in range(self._to_pull):
            next(self._predictions)

        # Reset counter
        self._to_pull = 0

    def _data_generator(self):
        self._cached_example = None
        self._to_pull = 0
        while not self._closed:
            try:
                example = self._data.pop(0)

                # Ensure examples used for padding match expected input format
                if isinstance(example, str):
                    self._cached_example = ""
                elif isinstance(example, (list, tuple)):
                    self._cached_example = [""] * len(example)

                yield example
            except IndexError:
                # _data_generator was asked for more examples than we had
                # Feed a cached example through the input_pipeline
                # to fill out the batch, but remember to clear it
                # out of the queue later
                self._to_pull += 1
                yield self._cached_example

    @contextmanager
    def cached_predict(self):
        """
        Context manager that prevents the recreation of the tensorflow graph on every call to BaseModel.predict().
        """
        self._cached_predict = True
        yield self
        self._cached_predict = False
        self.close()

    def _cached_inference(self, Xs, predict_keys=None, n_examples=None):
        """
        Ensure graph is not rebuilt on subsequent calls to .predict()
        """
        self._data = Xs
        self._closed = False
        n = n_examples or len(self._data)
        if self._predictions is None:
            input_fn = self.input_pipeline.get_predict_input_fn(self._data_generator)
            _estimator, hooks = self.get_estimator()
            self._predictions = _estimator.predict(
                input_fn=input_fn, predict_keys=predict_keys, hooks=hooks
            )

        self._clear_prediction_queue()

        predictions = [None] * n
        for i in tqdm.tqdm(range(n), total=n, desc="Inference"):
            y = next(self._predictions)
            try:
                y = y[predict_keys[0]] if len(predict_keys) == 1 else y
            except ValueError:
                raise FinetuneError(
                    "Cannot call `predict()` on a model that has not been fit."
                )
            predictions[i] = y

        return predictions

    def _inference(self, Xs, predict_keys=None, n_examples=None, context=None):
        Xs = self.input_pipeline._format_for_inference(Xs)

        if self._cached_predict:
            return self._cached_inference(
                Xs=Xs, predict_keys=predict_keys, n_examples=n_examples
            )
        else:
            input_fn = self.input_pipeline.get_predict_input_fn(Xs, context=context)
            estimator, hooks = self.get_estimator(
                build_explain=PredictMode.EXPLAIN in predict_keys
            )
            length = len(Xs) if not callable(Xs) else None

            predictions = tqdm.tqdm(
                estimator.predict(
                    input_fn=input_fn, predict_keys=predict_keys, hooks=hooks
                ),
                total=n_examples or length,
                desc="Inference",
            )
            try:
                return [
                    pred[predict_keys[0]] if len(predict_keys) == 1 else pred
                    for pred in predictions
                ]
            except ValueError:
                raise FinetuneError(
                    "Cannot call `predict()` on a model that has not been fit."
                )

    def fit(self, *args, **kwargs):
        """ An alias for finetune. """
        return self.finetune(*args, **kwargs)

    def _predict(self, Xs, context=None):
        raw_preds = self._inference(Xs, predict_keys=[PredictMode.NORMAL], context=context)
        return self.input_pipeline.label_encoder.inverse_transform(
            np.asarray(raw_preds)
        )

    def predict(self, Xs, context=None):
        return self._predict(Xs, context=context)

    def _predict_proba(self, Xs, context=None):
        """
        Produce raw numeric outputs for proba predictions
        """
        raw_preds = self._inference(Xs, predict_keys=[PredictMode.PROBAS], context=context)
        return raw_preds

    def predict_proba(self, *args, **kwargs):
        """
        The base method for predicting from the model.
        """
        raw_probas = self._predict_proba(*args, **kwargs)
        classes = self.input_pipeline.label_encoder.classes_

        formatted_predictions = []
        for probas in raw_probas:
            formatted_predictions.append(dict(zip(classes, probas)))
        return formatted_predictions

    def attention_weights(self, Xs):
        if self.config.base_model in [GPTModel, GPTModelSmall]:
            raw_preds = self._inference(Xs, predict_keys=[PredictMode.ATTENTION])
            return raw_preds
        raise NotImplementedError(
            "'attention_weights' only supported for GPTModel and GPTModelSmall base models."
        )
    
    def context_attention_weights(self, Xs, context=None):
        if not context:
            raise ValueError('Need to pass in context.')
        raw_preds = self._inference(Xs, context=context, predict_keys=[PredictMode.CONTEXT_ATTENTION])
        return raw_preds

    def _featurize(self, Xs, context=None):
        raw_preds = self._inference(Xs, context=context, predict_keys=[PredictMode.FEATURIZE])
        return np.asarray(raw_preds)

    def _featurize_sequence(self, Xs, context=None):
        raw_preds = self._inference(Xs, context=context, predict_keys=[PredictMode.SEQUENCE])
        return np.asarray(raw_preds)

    def featurize(self, *args, **kwargs):
        """
        Base method to get raw features out of the model.
        These features are the same features that are fed into the target_model.
        """
        return self._featurize(*args, **kwargs)

    def featurize_sequence(self, *args, **kwargs):
        """
        Base method to get raw token-level features out of the model.
        These features are the same features that are fed into the target_model.
        """
        return self._featurize_sequence(*args, **kwargs)

    @classmethod
    def get_eval_fn(cls):
        raise NotImplementedError(
            "No default eval function is given, please pass an explicit eval fn to grid_search"
        )

    def transform(self, *args, **kwargs):
        """
        An alias for `featurize`.
        """
        return self.featurize(*args, **kwargs)

    def _set_random_seed(self, seed=None):
        seed = seed or self.config.seed
        random.seed(seed)
        np.random.seed(seed)
        tf.set_random_seed(seed)

    def generate_text(self, seed_text="", max_length=None, use_extra_toks=None):
        """
        Performs a prediction on the Language modeling objective given some seed text. It uses a noisy greedy decoding.
        Temperature parameter for decoding is set in the config.
        :param max_length: The maximum length to decode to.
        :param seed_text: Defaults to the empty string. This will form the starting point to begin modelling
        :return: A string containing the generated text.
        """
        if use_extra_toks is None:
            use_extra_toks = self._trained
    
        def dataset_encoded():
            while not dataset_encoded.finished:
                yield {"tokens": arr_encoded.token_ids, "mask": arr_encoded.mask}

        dataset_encoded.finished = False

        def get_input_fn():
            types, shapes = self.input_pipeline.feed_shape_type_def()
            tf_dataset = Dataset.from_generator(dataset_encoded, types[0], shapes[0])
            return tf_dataset.batch(1)

        self.config.use_extra_toks = use_extra_toks
        encoded = self.input_pipeline.text_encoder._encode([seed_text])
        if encoded.token_ids == [] and not use_extra_toks:
            raise ValueError(
                "If you are not using the extra tokens, you must provide some non-empty seed text"
            )
        start = [self.input_pipeline.text_encoder.start_token] if use_extra_toks else []
        token_ids = start 
        if encoded.token_ids is not None and len(encoded.token_ids):
            token_ids += encoded.token_ids[0]
        encoded = EncodedOutput(token_ids=token_ids)

        estimator, hooks = self.get_estimator(force_build_lm=True)
        predict = estimator.predict(
            input_fn=get_input_fn, predict_keys=[PredictMode.GENERATE_TEXT], hooks=hooks
        )

        EOS = self.input_pipeline.text_encoder.end_token
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            for i in range(
                len(encoded.token_ids) - 1, (max_length or self.config.max_length) - 2
            ):
                arr_encoded = self.input_pipeline._array_format(encoded)
                class_idx = next(predict)[PredictMode.GENERATE_TEXT]
                encoded.token_ids.append(class_idx[-1])
                if encoded.token_ids[-1] == EOS:
                    break
            dataset_encoded.finished = True

        del self.config["use_extra_toks"]

        return self.input_pipeline.text_encoder.decode(encoded.token_ids)

    def __getstate__(self):
        """
        Leave serialization of all tf objects to tf
        """
        required_fields = ["_load_from_file", "config", "input_pipeline", "_trained"]
        serialized_state = {
            k: v for k, v in self.__dict__.items() if k in required_fields
        }
        return serialized_state

    def save(self, path):
        """
        Saves the state of the model to disk to the folder specific by `path`.  If `path` does not exist, it will be auto-created.

        Save is performed in two steps:
            - Serialize tf graph to disk using tf.Saver
            - Serialize python model using pickle

        Note:
            Does not serialize state of Adam optimizer.
            Should not be used to save / restore a training model.
        """
        if path is None:
            return
        
        if isinstance(path, str):
            path = os.path.abspath(path)
        self.saver.save(self, path)

    def create_base_model(self, filename, exists_ok=False):
        """
        Saves the current weights into the correct file format to be used as a base model.
        :param filename: the path to save the base model relative to finetune's base model filestore.
        :param exists_ok: Whether to replace the model if it exists.
        """
        base_model_path = os.path.join(os.path.dirname(__file__), "model", filename)

        if not exists_ok and os.path.exists(base_model_path):
            base_model_path = base_model_path + str(int(time.time()))
            LOGGER.warning(
                "Cannot overwrite model {}, set exists_ok to overwrite, saving as {} to avoid loss of data.".format(
                    filename, base_model_path
                )
            )

        if not self.saver.variables:
            raise FinetuneError(
                "Cannot save a base model with no weights changed. Call fit before creating a base model."
            )
        weights_stripped = {
            k: v
            for k, v in self.saver.variables.items()
            if "featurizer" in k and "Adam" not in k
        }
        joblib.dump(weights_stripped, base_model_path)

    def load(path, *args, **kwargs):
        """
        Load a saved fine-tuned model from disk.  Path provided should be a folder which contains .pkl and tf.Saver() files

        :param path: string path name to load model from.  Same value as previously provided to :meth:`save`. Must be a folder.
        :param **kwargs: key-value pairs of config items to override.
        """
        if type(path) != str and not hasattr(path, "write"):
            instance = path
            raise FinetuneError(
                'The .load() method can only be called on the class, not on an instance. Try `{}.load("{}") instead.'.format(
                    instance.__class__.__name__, args[0]
                )
            )

        assert_valid_config(**kwargs)

        saver = Saver()
        model = saver.load(path)

        # Backwards compatability
        # Ensure old models get new default settings
        for setting, default in get_default_config().items():
            if not hasattr(model.config, setting):
                if setting == "add_eos_bos_to_chunk":
                    model.config.add_eos_bos_to_chunk = False
                else:
                    model.config.update({setting: default})

        model.config.update(kwargs)
        model.input_pipeline.config = model.config
        download_data_if_required(model.config.base_model)
        saver.set_fallback(model.config.base_model_path)
        model._initialize()
        model.saver.variables = saver.variables
        model._trained = True
        return model


    @classmethod
    def finetune_grid_search(
        cls, Xs, Y, *, test_size, eval_fn=None, probs=False, return_all=False, **kwargs
    ):
        """
        Performs grid search over config items defined using "GridSearchable" objects and returns either full results or
        the config object that relates to the best results. The default config contains grid searchable objects for the
        most important parameters to search over.

        :param Xs: Input text. Either [num_samples] or [sequence, num_samples] for single or multi input models respectively.
        :param Y: Targets, A list of targets, [num_samples] that correspond to each sample in Xs.
        :param test_size: Int or float. If an int is given this number of samples is used to validate, if a float is
         given then that fraction of samples is used.
        :param eval_fn: An eval function that takes 2 inputs (prediction, truth) and returns a float, with a max value being desired.
        :param probs: If true, eval_fn is passed probability outputs from predict_proba, otherwise the output of predict is used.
        :param return_all: If True, all results are returned, if False, only the best config is returned.
        :param kwargs: Keyword arguments to pass to get_config()
        :return: default is to return the best config object. If return_all is true, it returns a list of tuples of the
            form [(config, eval_fn output), ... ]
        """
        if isinstance(Xs[0], str):
            Xs = [Xs]
        config = get_config(**kwargs)
        config.val_size = 0.0
        eval_fn = eval_fn or cls.get_eval_fn()

        trainXs, testXs, trainY, testY = train_test_split(
            list_transpose(Xs), Y, test_size=test_size, shuffle=True
        )
        trainXs = list_transpose(trainXs)
        testXs = list_transpose(testXs)
        gs = config.get_grid_searchable()
        ranged_keys = gs.keys()
        ranged_iterators = gs.values()
        grid_gen = itertools.product(*ranged_iterators)
        results = []
        for grid_item in grid_gen:
            config_ = deepcopy(config)
            config_.update(dict(zip(ranged_keys, grid_item)))
            instance = cls(config=config_)
            instance.finetune(*trainXs, Y=trainY)
            if probs:
                res = instance.predict_proba(*testXs)
            else:
                res = instance.predict(*testXs)
            results.append((config_, eval_fn(res, testY)))
            del instance

        if return_all:
            return results
        return max(results, key=lambda x: x[1])[0]

    @classmethod
    def finetune_grid_search_cv(
        cls,
        Xs,
        Y,
        *,
        n_splits,
        test_size,
        eval_fn=None,
        probs=False,
        return_all=False,
        **kwargs
    ):
        """
        Performs cross validated grid search over config items defined using "GridSearchable" objects and returns either full results or
        the config object that relates to the best results. The default config contains grid searchable objects for the
        most important parameters to search over.

        It should be noted that the cv splits are not guaranteed unique, but each split is given to each set of hparams.

        :param Xs: Input text. Either [num_samples] or [sequence, num_samples] for single or multi input models respectively.
        :param Y: Targets, A list of targets, [num_samples] that correspond to each sample in Xs.
        :param n_splits: Number of CV splits to do.
        :param test_size: Int or float. If an int is given this number of samples is used to validate, if a float is
            given then that fraction of samples is used.
        :param eval_fn: An eval function that takes 2 batches of outputs and returns a float, with a max value being
            desired. An arithmetic mean must make sense for this metric.
        :param probs: If true, eval_fn is passed probability outputs from predict_proba, otherwise the output of predict is used.
        :param return_all: If True, all results are returned, if False, only the best config is returned.
        :param kwargs: Keyword arguments to pass to get_config()
        :return: default is to return the best config object. If return_all is true, it returns a list of tuples of the
            form [(config, eval_fn output), ... ]
        """
        results = []
        for _ in range(n_splits):
            res = cls.finetune_grid_search(
                Xs,
                Y,
                test_size=test_size,
                probs=probs,
                eval_fn=eval_fn,
                return_all=True,
                **kwargs
            )
            results.append(res)
        results = list(zip(*results))
        aggregated_results = []
        for configuration in results:
            config_common = None
            sum_res = 0
            n_res = 0
            for config, result in configuration:
                config_common = config_common or config
                assert config == config_common
                n_res += 1
                sum_res += result
            aggregated_results.append((config_common, sum_res / n_res))

        if return_all:
            return aggregated_results

        return max(aggregated_results, key=lambda x: x[1])[0]

    def process_long_sequence(self, X, context=None):
        arr_encoded = [
            self.input_pipeline._text_to_ids(x) for x in self.input_pipeline._format_for_inference(X)
        ]

        flat_array_encoded = []
        sequence_id = []
        for i, ae in enumerate(arr_encoded):
            for sample in ae:
                flat_array_encoded.append(sample)
                sequence_id.append(i)

        labels, batch_probas = [], []
        for pred in self._inference(X, predict_keys=[PredictMode.PROBAS, PredictMode.NORMAL], n_examples=len(flat_array_encoded), context=context):
            normal_pred = pred[PredictMode.NORMAL]
            if not hasattr(self, 'multi_label'):
                normal_pred = np.expand_dims(normal_pred, 0)
            labels.append(self.input_pipeline.label_encoder.inverse_transform(normal_pred))
            batch_probas.append(pred[PredictMode.PROBAS])

        if not batch_probas:
            batch_probas = [None]*len(labels)

        for chunk_idx, (label_seq, proba_seq) in enumerate(zip(labels, batch_probas)):
            position_seq = flat_array_encoded[chunk_idx].char_locs
            start_of_doc = chunk_idx == 0 or sequence_id[chunk_idx - 1] != sequence_id[chunk_idx]
            end_of_doc = (
                chunk_idx + 1 == len(flat_array_encoded) or
                sequence_id[chunk_idx] != sequence_id[chunk_idx + 1]
            )
            yield position_seq, start_of_doc, end_of_doc, label_seq, proba_seq

    def __del__(self):
        if hasattr(self, "_tmp_dir") and self._tmp_dir is not None:
            self._tmp_dir.cleanup()
