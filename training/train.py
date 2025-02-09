from utils import *
from args_parse import main as args_parse
from torch.utils.data import DataLoader
from torch.optim import AdamW
from tqdm.auto import tqdm
from transformers import get_linear_schedule_with_warmup, T5ForConditionalGeneration
from evaluating import evaluate
from transformers import DataCollatorWithPadding
from accelerate import Accelerator
from checkpoint import save_checkpoint, load_checkpoint
import numpy as np



def train(args, train_dataloader, eval_dataloader, model, tokenizer, accelerator):
    # Setup
    args.max_steps = args.epoch * len(train_dataloader)
    args.save_steps = len(train_dataloader)
    args.num_train_epochs = args.epoch
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
         'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    if args.warmup_steps == 0:
        num_warmup = args.max_steps * args.warmup_ratio
    else:
        num_warmup = args.warmup_steps

    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=num_warmup,
                                                num_training_steps=args.max_steps)
    criterion = torch.nn.CrossEntropyLoss(ignore_index=0)


    global_step = args.start_step
    tr_loss, logging_loss, avg_loss, tr_nb, tr_num, train_loss = 0.0, 0.0, 0.0, 0, 0, 0
    best_bleu_score = 0.0
    patience = 0

    args.device = accelerator.device
    model, optimizer, train_dataloader, eval_dataloader, scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, eval_dataloader, scheduler
    )
    # load_checkpoint(args, accelerator, 'checkpoint-best-acc')

    # Train
    if accelerator.is_main_process:
        logging.info("***** Running training *****")
        logging.info(f"  Num examples = {len(train_dataloader) * args.train_batch_size}")
        logging.info(f"  Num Epochs = {args.num_train_epochs}")
        logging.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")

    model.zero_grad()

    step = 0
    for idx in range(args.start_epoch, int(args.num_train_epochs)):
        bar = tqdm(train_dataloader, total=len(train_dataloader), disable=not accelerator.is_local_main_process)
        tr_num = 0
        train_loss = 0

        for _, batch in enumerate(bar):
            with accelerator.accumulate(model):
                in_ids = batch['source_ids'].to(args.device)
                in_masks = batch['source_mask'].to(args.device)
                target_ids = batch['target_ids'].to(args.device)

                model.train()

                outputs  = model(in_ids, in_masks, target_ids)

                loss = criterion(outputs.logits.view(-1, outputs.logits.size(-1)), target_ids.view(-1))

                accelerator.backward(loss)
                if accelerator.sync_gradients:
                    accelerator.clip_grad_value_(model.parameters(), args.max_grad_norm)

                tr_loss += loss.item()
                tr_num += 1
                train_loss += loss.item()
                if avg_loss == 0:
                    avg_loss = tr_loss
                avg_loss = round(train_loss / tr_num, 5)
                bar.set_description(f"Epoch {idx} - Loss: {avg_loss}")

                optimizer.step()
                optimizer.zero_grad()
                scheduler.step()
                global_step += 1
                avg_loss = round(np.exp((tr_loss - logging_loss) / (global_step - tr_nb)), 4)
                if args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    logging_loss = tr_loss
                    tr_nb = global_step

                # log after every logging_steps (e.g., 1000)
                if (step + 1) % args.logging_steps == 0:
                    avg_loss = round(train_loss / tr_num, 5)
                    if args.evaluate_during_training:
                        results = evaluate(args, model, eval_dataloader, tokenizer, accelerator)
                        if accelerator.is_main_process:
                            for key, value in results.items():
                                logging.info("  %s = %s", key, round(value, 4))
                        valid_loss, valid_bleu_score = results.values()

                        accelerator.log({
                            'Loss/train-per-1000-steps': avg_loss,
                            'Loss/valid-per-1000-steps': valid_loss,
                            'Bleu_score/valid-per-1000-steps': valid_bleu_score,
                        }, step=step)

                        # Save model checkpoint
                        if results['eval_bleu_score'] > best_bleu_score:
                            best_bleu_score = results['eval_bleu_score']
                            if accelerator.is_main_process:
                                logging.info("  " + "*" * 20)
                                logging.info("  Best bleu score:%s", round(best_bleu_score, 4))
                                logging.info("  " + "*" * 20)
                            save_checkpoint(args, accelerator, 'checkpoint-best-bleu-score')

                # increment step within the same epoch
                step += 1

        # log after every epoch
        avg_loss = round(train_loss / tr_num, 5)

        if args.evaluate_during_training:  # Only evaluate when single GPU otherwise metrics may not average well
            results = evaluate(args, model, eval_dataloader, tokenizer, accelerator)
            if accelerator.is_main_process:
                for key, value in results.items():
                    logging.info("  %s = %s", key, round(value, 4))
            valid_loss, valid_bleu_score= results.values()

            accelerator.log({
                'Loss/train-per-epoch': avg_loss,
                'Loss/valid-per-epoch': valid_loss,
                'Bleu_score/valid-per-1000-steps': valid_bleu_score,
            }, step=step)

            # save model checkpoint at ep10
            if idx == 9:
                save_checkpoint(args, accelerator, f'checkpoint-epoch-{idx + 1}')

            # Save model checkpoint
            if results['eval_bleu_score'] > best_bleu_score:
                best_bleu_score = results['eval_bleu_score']
                if accelerator.is_main_process:
                    logging.info("  " + "*" * 20)
                    logging.info("  Best Bleu Score:%s", round(best_bleu_score, 4))
                    logging.info("  " + "*" * 20)
                save_checkpoint(args, accelerator, 'checkpoint-best-bleu-score')
                patience = 0
            else:
                patience += 1

        if patience == args.max_patience:
            if accelerator.is_main_process:
                logging.info(f"Reached max patience ({args.max_patience}). End training now.")
            if best_bleu_score == 0.0:
                save_checkpoint(args, accelerator, 'checkpoint-best-bleu-score')
            break

    # Final Evaluation
    results = {}
    if args.do_eval:
        load_checkpoint(args, accelerator, 'checkpoint-best-bleu-score')
        result = evaluate(args, model, eval_dataloader, tokenizer, accelerator)
        if accelerator.is_main_process:
            logging.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logging.info(f"  {key} = {str(round(result[key], 4))}")

    accelerator.end_training()

    return results

def main(args):
    accelerator = Accelerator(gradient_accumulation_steps=args.gradient_accumulation_steps, log_with="wandb")

    accelerator.init_trackers(
        project_name=args.project,
        config = {
            "learning_rate" : args.leraning_rate,
            "train_batch_size" : args.train_batch_size,
            "eval_batch_size" : args.eval_batch_size,
            "gradient_accumulation_steps" : args.gradient_accumulation_steps,
            "adam_epsilon" : args.adam_epsilon,
            "num_train_epochs" : args.num_train_epochs,
            "warmup_steps" : args.warmup_steps,
            "warmup_ratio" : args.warmup_ratio,
            "max_patience" : args.max_patience,
            "max_grad_norm" : args.max_grad_norm,
            "logging_steps" : args.logging_steps,
            "save_steps" : args.save_steps,
            "weight_decay" : args.weight_decay,
            "seed" : args.seed,
            "fp16" : args.fp16,
        },
        init_kwargs={"wandb": {"entity": "Kien-HUST-MI2"}}
    )

    seed_torch(args.seed)

    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    if accelerator.is_main_process:
        logging.debug(config_class)
        logging.debug(model_class)
        logging.debug(tokenizer_class)

    config = config_class.from_pretrained(args.config_name if args.config_name else args.model_name_or_path, cache_dir=args.cache_dir if args.cache_dir else None)
    config.num_labels = 2

    if accelerator.is_main_process:
        logging.debug(config)

    tokenizer = tokenizer_class.from_pretrained(args.tokenizer_name, do_lower_case=args.do_lower_case, cache_dir=args.cache_dir if args.cache_dir else None)

    if tokenizer.pad_token == None:
        tokenizer.pad_token = (tokenizer.eos_token)
        tokenizer.pad_token_id = tokenizer(tokenizer.pad_token, truncation=True)['input_ids'][0]

    if args.block_size <= 0:
        args.block_size = tokenizer.max_len_single_sentence  # Our input block size will be the max possible for the model
    args.block_size = min(args.block_size, tokenizer.max_len_single_sentence)

    model = T5ForConditionalGeneration.from_pretrained(args.model_name_or_path)    #Change this to choose another model for evaluating

    if accelerator.is_main_process:
        logging.debug(model)

    #Load data
    train_data = load_jsonl(args.train_data_file)
    eval_data = load_jsonl(args.eval_data_file)
    if accelerator.is_main_process:
        logging.info(f"Total train data: {len(train_data)}")
        logging.info(f"Total validate data: {len(eval_data)}")

    #Dataloader
    data_collator = DataCollatorWithPadding(tokenizer = tokenizer)

    train_set_loader = DataLoader(train_data, batch_size=args.train_batch_size, shuffle=True, collate_fn=data_collator)
    eval_set_loader = DataLoader(eval_data, batch_size=args.eval_batch_size, shuffle=False, collate_fn=data_collator)

    #Training
    results = train(args, model, train_set_loader, eval_set_loader, tokenizer, accelerator)

    return results

if __name__ == '__main__':
    args = args_parse()
    args.start_epoch = 0
    args.start_step = 0
    main(args)



