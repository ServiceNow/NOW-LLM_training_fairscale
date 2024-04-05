import math
import sys
import contextlib

import torch

import accessory.util.misc as misc
import accessory.util.lr_sched as lr_sched

from fairscale.nn.model_parallel import initialize as fs_init


def train_one_epoch(model: torch.nn.Module,
                    data_loader, optimizer: torch.optim.Optimizer,
                    epoch: int, start_iter: int, loss_scaler,
                    log_writer=None,
                    args=None):
    model.train(True)
    metric_logger = misc.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', misc.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 10

    accum_iter = args.accum_iter

    model.zero_grad(set_to_none=True)

    if log_writer is not None and args.log_to == "tensorboard":
        print('log_dir: {}'.format(log_writer.log_dir))
    for data_iter_step, batch_data in enumerate(
        metric_logger.log_every(data_loader, print_freq, header, start_iter), start=start_iter):

        if data_iter_step % accum_iter == 0:
            lr_sched.adjust_learning_rate_epoch(optimizer, data_iter_step / len(data_loader) + epoch, args)

        autocast_ctx = {
            "bf16": torch.cuda.amp.autocast(dtype=torch.bfloat16),
            "fp16": torch.cuda.amp.autocast(dtype=torch.float16),
            "tf32": contextlib.nullcontext(),
        }[args.precision]
        with autocast_ctx:
            
            dpo_output, additional_loss_dict = model(
                examples=batch_data["input_ids"],
                labels=batch_data["labels"],
                masks=batch_data["input_masks"],
                kl_examples=batch_data["kl_input_ids"],
                kl_labels=batch_data["kl_labels"],
                kl_masks=batch_data["kl_input_masks"],
                ref_logps=batch_data["ref_logps"],
                ref_kl_logps=batch_data["kl_ref_logps"],
                tags=batch_data["tag"]
                )
        loss = dpo_output["loss"]

        # how do we wanna weigh the load balancing loss in the MOE WRT DPO? First read how it is done in literature
        for (add_loss, weight) in additional_loss_dict.values():
            loss = loss + add_loss * weight
        loss_value = loss.item()
        dpo_loss_value = dpo_output["loss"].item()
        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            sys.exit(1)

        loss /= accum_iter

        update_grad = (data_iter_step + 1) % accum_iter == 0
        grad_norm = loss_scaler(
            loss, optimizer, model,
            parameters=model.parameters(),
            update_grad=update_grad,
            clip_grad=None if args.clip_grad <= 0 else args.clip_grad,
        )

        if update_grad:
            assert grad_norm is not None
            if torch.any(torch.isinf(grad_norm)):
                print("grad norm is inf")
            else:
                metric_logger.update(grad_norm=grad_norm)

            model.zero_grad(set_to_none=True)

        torch.cuda.synchronize()

        metric_logger.update(dpo_loss=dpo_loss_value)
        metric_logger.update(**{key: val[0].item() for key, val in additional_loss_dict.items()})
        metric_logger.update(**{key: val.item() for key, val in dpo_output.items()})

        lr = optimizer.param_groups[0]["lr"]
        metric_logger.update(lr=lr)

        if (data_iter_step + 1) % args.log_steps == 0:
            for metric_name, metric in metric_logger.meters.items():
                metric_value = metric.value
                metric_value = misc.all_reduce_mean(metric_value, group=fs_init.get_data_parallel_group())
                if log_writer is not None:
                    if args.log_to == "wandb":
                        log_writer.log({metric_name: metric_value}, step=data_iter_step + len(data_loader) * epoch)
                    else:
                        log_writer.add_scalar(metric_name, metric_value, data_iter_step + len(data_loader) * epoch)

        # save within epoch
        n_update_per_save = args.save_iteration_interval // accum_iter
        if update_grad and ((data_iter_step + 1) // accum_iter) % n_update_per_save == 0:
            misc.save_checkpoint(
                output_dir=args.output_dir,
                args=args, epoch=epoch, iteration=data_iter_step, model=model, optimizer=optimizer,
                loss_scaler=loss_scaler, dataset_state=None,
            )

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}