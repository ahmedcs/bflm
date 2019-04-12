#!/usr/bin/env python
# coding: utf-8

import argparse
import logging
import math
import os
import time

import numpy as np
import torch
import torch.nn.functional as F
import tqdm
from tensorboardX import SummaryWriter
from torch.utils.data import DataLoader, Dataset
from tqdm import trange

import pytorch_pretrained_bert
from data_loader import get_data_loader
from model_sampler import print_samples
from pytorch_pretrained_bert import GPT2LMHeadModel, GPT2Tokenizer, OpenAIAdam


def log_tb(tag, val):
  """Log value to tensorboard (relies on global_example_count rather than step count to give comparable graphs across batch sizes)"""
  global global_example_count, event_writer
  event_writer.add_scalar(tag, val, global_example_count)


def checkpoint(model, args):
    output_model_file = os.path.join(args.output_dir, "pytorch_model.bin")
    print('saving checkpoint to', output_model_file)
    # Only save the model itself
    model_to_save = model.module if hasattr(model, 'module') else model  
    torch.save(model_to_save.state_dict(), output_model_file)


def main():
    global global_example_count, event_writer

    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--model_name_or_path', type=str, default='gpt2',
                        help='pretrained model name')
    parser.add_argument("--do_train", action='store_true', help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true', help="Whether to run eval on the dev set.")
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument('--train_dataset', type=str, default='')
    parser.add_argument('--eval_dataset', type=str, default='')
    parser.add_argument('--context_length', type=int, default=128)
    parser.add_argument('--num_train_epochs', type=int, default=3)
    parser.add_argument('--train_batch_size', type=int, default=16)
    parser.add_argument('--eval_batch_size', type=int, default=16)
    parser.add_argument('--max_grad_norm', type=int, default=1)
    parser.add_argument('--learning_rate', type=float, default=1e-2)
    parser.add_argument('--warmup_proportion', type=float, default=0.002)
    parser.add_argument('--lr_schedule', type=str, default='warmup_linear')
    parser.add_argument('--weight_decay', type=float, default=0.01)
    parser.add_argument('--n_valid', type=int, default=374)
    parser.add_argument('--server_ip', type=str, default='', help="Can be used for distant debugging.")
    parser.add_argument('--server_port', type=str, default='', help="Can be used for distant debugging.")
    parser.add_argument('--distributed', action='store_true', help='Run distributed training')
    parser.add_argument('--logdir',type=str, default='/tmp/runs', help="location of logging directory")
    
    args = parser.parse_args()
    os.system('mkdir -p ' + args.logdir)

    torch.random.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    args.device = device

    enc = GPT2Tokenizer.from_pretrained(args.model_name_or_path)
    model = GPT2LMHeadModel.from_pretrained(args.model_name_or_path)
    model.to(device)

    # setup TensorBoard logging
    global_example_count = 0
    print(f"Logging to {args.logdir}")
    event_writer = SummaryWriter(args.logdir)
    log_tb("first", time.time())

    if args.do_train:
        data_loader = get_data_loader(args.train_dataset, enc, args.train_batch_size, args)

        # ## Prep optimizer
        # We use OpenAIAdam because that's what run_openai_gpt used
        # Prepare optimizer
        param_optimizer = list(model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
            ]
        num_train_optimization_steps = len(data_loader) * args.num_train_epochs

        optimizer = OpenAIAdam(optimizer_grouped_parameters,
                            lr=args.learning_rate,
                            warmup=args.warmup_proportion,
                            max_grad_norm=args.max_grad_norm,
                            weight_decay=args.weight_decay,
                            t_total=num_train_optimization_steps)


        # ## Train loop
        # Based on `run_openai_gpt.py`
        nb_tr_steps, tr_loss, exp_average_loss = 0, 0, None

        # Reset all model weights so we can train from scratch.
        model.apply(model.init_weights)

        try:
            for _ in trange(int(args.num_train_epochs), desc="Epoch"):
                tr_loss = 0
                nb_tr_steps = 0
                tqdm_bar = tqdm.tqdm(data_loader, desc="Training")
                for step, batch in enumerate(tqdm_bar):
                    # Put model in training mode.
                    model.train()
                    batch = batch.to(device)
                    # input_ids, position_ids=None, token_type_ids=None, lm_labels=None, past=None
                    # if lm_labels, outputs loss
                    loss = model(batch, lm_labels=batch)
                    loss.backward()
                    optimizer.step()
                    optimizer.zero_grad()
                    tr_loss += loss.item()
                    exp_average_loss = loss.item() if exp_average_loss is None else 0.7*exp_average_loss+0.3*loss.item()
                    nb_tr_steps += 1
                    tqdm_bar.desc = "Training loss: {:.2e} lr: {:.2e}".format(exp_average_loss, optimizer.get_lr()[0])
                    log_tb('loss', loss.item())
                    log_tb('lr', optimizer.get_lr()[0])
                    global_example_count+=args.train_batch_size


        except KeyboardInterrupt:
            tqdm_bar.close()
        finally:
            print_samples(model, enc, args, context_tokens=next(iter(data_loader)), batch_size=1, length=20, nsamples=1, 
                    temperature=1, top_k=40)
            checkpoint(model, args)

    if args.do_eval:
        data_loader = get_data_loader(args.eval_dataset, enc, args.eval_batch_size, args)
        model.eval()
        nb_steps, eval_loss, exp_average_loss = 0, 0, None
        with torch.no_grad():
            tqdm_bar = tqdm.tqdm(data_loader, desc="Eval")
            for step, batch in enumerate(tqdm_bar):
                # Put model in training mode.
                batch = batch.to(device)
                # input_ids, position_ids=None, token_type_ids=None, lm_labels=None, past=None
                # if lm_labels, outputs loss
                loss = model(batch, lm_labels=batch)
                eval_loss += loss.item()
                exp_average_loss = loss.item() if exp_average_loss is None else 0.7*exp_average_loss+0.3*loss.item()
                nb_steps += 1
                tqdm_bar.desc = "Eval loss: {:.2e} ppl: {:.2e}".format(exp_average_loss, math.exp(exp_average_loss))
                log_tb('loss', loss.item())
                log_tb('ppl', loss.exp().item())
                global_example_count+=args.train_batch_size
        print('Final ppl:', math.exp(eval_loss / nb_steps))



if __name__ == '__main__':
    main()
