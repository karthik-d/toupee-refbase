#!/usr/bin/python
"""
Run a MLP experiment from a yaml file

Alan Mosca
Department of Computer Science and Information Systems
Birkbeck, University of London

All code released under GPLv2.0 licensing.
"""
__docformat__ = 'restructedtext en'

import os
import argparse
import logging
import toupee as tp

def main(args=None):
    """ Train a base model as specified """
    parser = argparse.ArgumentParser(description='Train a single Base Model')
    parser.add_argument('params_file', help='the parameters file')
    parser.add_argument('save_file', nargs='?',
                        help='the file where the trained MLP is to be saved')
    parser.add_argument('--epochs', type=int, nargs='?',
                        help='number of epochs to run')
    parser.add_argument('--wandb', action="store_true",
                        help="Send results to Weights and Biases")
    parser.add_argument('--wandb-project', type=str, help="Weights and Biases project name")
    parser.add_argument('--wandb-group', type=str, help="Weights and Biases group name")
    args = parser.parse_args(args)
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
    logging.info(("using toupee version {0}".format(tp.version)))
    params = tp.config.load_parameters(args.params_file)
    if args.wandb:
        import wandb
        wandb_project = args.wandb_project or f"toupee-{params.dataset}-base_model"
        group_id = wandb.util.generate_id()
        wandb_group = args.wandb_group or f"toupee-{params.dataset}-{group_id}"
        wandb.init(project=wandb_project,
                   config={"type": "base_model", "args": args, "params": params.__dict__},
                   group=wandb_group,
                   name='model-0')
    data = tp.data.Dataset(src_dir=params.dataset, **params.__dict__)
    base_model = tp.model.Model(params=params)
    base_model.fit(data=data, log_wandb=args.wandb)
    logging.info(base_model.test_metrics['classification_report'])
    tp.utils.pretty_print_confusion_matrix(base_model.test_metrics['confusion_matrix'])
    logging.info('\n{:*^40}'.format(" Model Metrics "))
    for metric_name in tp.PRINTABLE_METRICS:
        logging.info(f"{metric_name}: {base_model.test_metrics[metric_name]}")
    if args.save_file:
        base_model.save(args.save_file)

if __name__ == '__main__':
    main()
