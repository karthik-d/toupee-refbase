#!/usr/bin/python
"""
Run a MLP experiment from a yaml file

Alan Mosca
Department of Computer Science and Information Systems
Birkbeck, University of London

All code released under Apachev2.0 licensing.
"""
__docformat__ = 'restructedtext en'

import time
import copy
import logging
import tensorflow as tf # type: ignore
import numpy as np # type: ignore
import uuid
import yaml
import json

try:
    from yaml import CLoader as YLoader
except ImportError:
    from yaml import Loader as YLoader

import wandb
import toupee as tp
#TODO: backprop to the inputs
#TODO: sample weights
#TODO: early stopping
#TODO: checkpointing?


class OptimizerSchedulerCallback(tf.keras.callbacks.Callback):

    def __init__(self, optimizer_schedule):
        super(OptimizerSchedulerCallback, self).__init__()
        self.optimizer_schedule = optimizer_schedule

    def on_epoch_end(self, epoch, logs=None):
        """ Callback to stop training and change the optimizer """
        epoch_keys = self.optimizer_schedule.params.keys()
        if epoch + 1 in epoch_keys:
            optimizer = self.optimizer_schedule[epoch+1]
            self.model.compile(optimizer, loss=self.model.loss, metrics=self.model.metrics)


class OptimizerSchedule:
    """ Schedules Optimizers and Learning Rates according to config """
    def __init__(self, params, epochs: int):
        """ Create an optimizer from params and learning rate """
        self.params = copy.deepcopy(params)
        self.optimizers = {}
        self.epochs = epochs
        if 'class_name' in self.params: # Force this to be an epoch schedule even if it's not
            self.params = {0: self.params}
        for thresh, opt_params in self.params.items():
            conf = copy.deepcopy(opt_params)
            if isinstance(conf['config']['learning_rate'], dict):
                lr = conf['config']['learning_rate']
                conf['config']['learning_rate'] = lr[min(lr.keys())]
            self.optimizers[thresh] = tf.keras.optimizers.deserialize(conf)
        self.lr_callback = tf.keras.callbacks.LearningRateScheduler(self._lr_scheduler)
    
    def _params_scheduler(self, epoch: int):
        for thresh in sorted(self.params.keys(), reverse=True):
            if epoch >= thresh:
                return self.params[thresh]

    def _opt_scheduler(self, epoch: int):
        for thresh in sorted(self.optimizers.keys(), reverse=True):
            if epoch >= thresh:
                return self.optimizers[thresh]
    
    def __getitem__(self, epoch: int):
        """
        Subscripting operator, so we can use [] to get the right
        optimizer for any epoch
        """ 
        return self._opt_scheduler(epoch)

    def _lr_scheduler(self, epoch: int):
        #TODO: use sorting
        lr = None
        params = self._params_scheduler(epoch)
        if isinstance(params['config']['learning_rate'], dict):
            for thresh, value in params['config']['learning_rate'].items():
                if epoch >= thresh:
                    lr = value
        else:
            lr = params['config']['learning_rate']
        return lr

    def get_callbacks(self, loss, metrics):
        return [self.lr_callback,
                OptimizerSchedulerCallback(self)]


class Model:
    """ Representation of a model """
    #TODO: Frozen layers
    #TODO: Get model id and use different tb log dir for each model
    def __init__(self, params, model_yaml=None, optimizer=None):
        self.params = params

        # PIPELINE FIX: Newer versions of `tf` do not allow `yaml`` configuration for models. 
        # Following lines convert `yaml` to `json` for model configuration.
        '''
        # Previous Version
        if not model_yaml:
            with open(params.model_file, 'r') as model_file:
                self.model_yaml = model_file.read()
        else:
            self.model_yaml = model_yaml
        '''
        with open(params.model_file, 'r') as model_file:
            configuration = yaml.load(model_file.read(), Loader=YLoader)
        
        self.model_json = json.dumps(configuration, indent=2)
        self._model = tf.keras.models.model_from_json(self.model_json)

        if params.model_weights:
            self._model.load_weights(params.model_weights)
        self.optimizer = optimizer or params.optimizer
        self._optimizer_schedule = OptimizerSchedule(self.optimizer, self.params.epochs)
        self._loss = tf.keras.losses.deserialize(params.loss)
        self.params = params
        self._training_metrics = ['accuracy']

    def inject_layers(self, additional_layers, predecessor):
        """ Add k layers in position P """
        model_config = self._model.get_config()
        new_layers = []
        inject_uuid = uuid.uuid1()
        inserted = False
        for layer in model_config['layers']:
            if not inserted:
                new_layers.append(layer)
                if layer['name'] == predecessor:
                    # using PREDECESSOR to refer to the predecessor layer is a convention
                    replacement_mappings = {'PREDECESSOR': predecessor}
                    for i, new_layer in enumerate(copy.deepcopy(additional_layers)):
                        new_name = f"autogenerated-{new_layer['name']}-{inject_uuid}-{i}"
                        replacement_mappings[new_layer['name']] = new_name
                        new_layer['name'] = new_name
                        new_layer['config']['name'] = new_name
                        # this is delicate: we need to make sure that all the layers are connected
                        # with their new autogenerated names
                        for replace_old, replace_new in replacement_mappings.items():
                            new_layer['inbound_nodes'] = tp.utils.replace_inbound_layer(
                                new_layer['inbound_nodes'], replace_old, replace_new
                            )
                        new_layers.append(new_layer)
                        last_new_layer = new_layer['name']
                    inserted = True
            else:
                layer['inbound_nodes'] = tp.utils.replace_inbound_layer(layer['inbound_nodes'], predecessor, last_new_layer)
                new_layers.append(layer)
        model_config['layers'] = new_layers
        self._model = tf.keras.Model.from_config(model_config)
        
        # CHANGE: json-yaml switch
        # self.model_yaml = self._model.to_yaml()
        self.model_json = self._model.to_json()
        
        return last_new_layer

    def copy_weights(self, other_model, early_stop=False):
        """ Copy weights from another model, up to the last layer with the same name """
        for this_layer, other_layer in zip(self._model.layers, other_model._model.layers):
            if this_layer.name != other_layer.name:
                if early_stop:
                    break
                else:
                    continue
            this_layer.set_weights(other_layer.get_weights())

    def fit(self, data: tp.data.Dataset, epochs=None, verbose=None, log_wandb:bool=False,
            adversarial_testing:bool=False, tensorboard=False):
        """ Train a model """
        start_time = time.perf_counter()
        callbacks = self._optimizer_schedule.get_callbacks(self._loss,
                                                           self._training_metrics)
        if self.params.reduce_lr_on_plateau:
            callbacks.append(
                tf.keras.callbacks.ReduceLROnPlateau(**self.params.reduce_lr_on_plateau))
        if log_wandb:
            callbacks.append(wandb.keras.WandbCallback())
        if tensorboard:
            callbacks.append(tf.keras.callbacks.TensorBoard(log_dir=self.params.tb_log_dir))
        if self.params.multi_gpu:
            logging.warning("!!! WARNING - EXPERIMENTAL !!! running on multi gpu")
            tf.keras.utils.multi_gpu_model(self._model, gpus=self.params.multi_gpu)
        self.img_gen = data.img_gen
        self._model.compile(
            optimizer = self._optimizer_schedule[0],
            loss = self._loss,
            metrics = self._training_metrics,
            )
        self._model.fit(
            data.get_training_handle(),
            epochs = epochs or self.params.epochs,
            steps_per_epoch = data.steps_per_epoch['train'],
            shuffle = 'batch',
            callbacks = callbacks,
            verbose = verbose or self.params.verbose,
            validation_data = data.get_validation_handle(standardized=True),
            )
        end_time = time.perf_counter()
        logging.info('Model trained for %.2fm' % ((end_time - start_time) / 60.))
        self.test_metrics = self.evaluate(data.get_testing_handle(), adversarial=adversarial_testing)
        if log_wandb:
            for metric, value in self.test_metrics.items():
                # for some reason MyPy doesn't understand this module well
                wandb.run.summary[metric] = value  # type: ignore

    def evaluate(self, test_data, adversarial:bool=False):
        """ Evaluate model on some test data handle """
        return tp.metrics.evaluate(self, test_data, adversarial_gradient_source=self if adversarial else None)

    def predict_proba(self, X):
        """ Output logits """
        if self.img_gen:
            X = self.img_gen.standardize(X)
        return self._model.predict(X)

    def predict_classes(self, X):
        """ Aggregated argmax """
        return np.argmax(self.predict_proba(X), axis = 1)

    def save(self, filename):
        """ Train a model """
        self._model.save(filename)

    def get_keras_model(self):
        """ Return raw Keras model """
        return self._model
