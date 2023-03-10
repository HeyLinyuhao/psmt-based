import torch
from tqdm import tqdm
from math import ceil
from Utils.ramps import *
from itertools import cycle
import torch.nn.functional as F
import torch.distributed as dist
from collections import OrderedDict
from Utils.pyt_utils import PostAug
from Base.base_trainer import BaseTrainer
from Utils.metrics import eval_metrics, AverageMeter


class Trainer(BaseTrainer):
    def __init__(self, model, config, supervised_loader, unsupervised_loader,  iter_per_epoch,
                 pretrain_loader=None, val_loader=None, train_logger=None, wandb_run=None, args=None):
        super(Trainer, self).__init__(model, config, iter_per_epoch, train_logger, args)

        self.supervised_loader = supervised_loader
        self.unsupervised_loader = unsupervised_loader
        self.pretrain_loader = pretrain_loader
        self.val_loader = val_loader
        self.args = args
        self.tensor_board = wandb_run if self.args.local_rank <= 0 else None
        self.iter_per_epoch = iter_per_epoch
        self.ignore_index = self.val_loader.dataset.ignore_index
        self.log_step = config['trainer'].get('log_per_iter', int(np.sqrt(self.val_loader.batch_size)))
        if config['trainer']['log_per_iter']:
            self.log_step = int(self.log_step / self.val_loader.batch_size) + 1
        self.num_classes = self.val_loader.dataset.num_classes
        self.mode = self.model.module.mode
        self.gamma = config['trainer']['gamma']
        self.post_aug_process = PostAug(width_size=config['train_unsupervised']['crop_size'],
                                        height_size=config['train_unsupervised']['crop_size'])

    @torch.no_grad()
    def update_teachers(self, teacher_encoder, teacher_decoder, keep_rate=0.996):
        student_encoder_dict = self.model.module.encoder_s.state_dict()
        student_decoder_dict = self.model.module.decoder_s.state_dict()
        new_teacher_encoder_dict = OrderedDict()
        new_teacher_decoder_dict = OrderedDict()

        for key, value in teacher_encoder.state_dict().items():

            if key in student_encoder_dict.keys():
                new_teacher_encoder_dict[key] = (
                        student_encoder_dict[key] * (1 - keep_rate) + value * keep_rate
                )
            else:
                raise Exception("{} is not found in student encoder model".format(key))

        for key, value in teacher_decoder.state_dict().items():

            if key in student_decoder_dict.keys():
                new_teacher_decoder_dict[key] = (
                        student_decoder_dict[key] * (1 - keep_rate) + value * keep_rate
                )
            else:
                raise Exception("{} is not found in student decoder model".format(key))
        teacher_encoder.load_state_dict(new_teacher_encoder_dict, strict=True)
        teacher_decoder.load_state_dict(new_teacher_decoder_dict, strict=True)

    @staticmethod
    def rand_bbox_1(size, lam=None):
        # past implementation
        W = size[2]
        H = size[3]
        B = size[0]
        cut_rat = np.sqrt(1. - lam)
        cut_w = np.int(W * cut_rat)
        cut_h = np.int(H * cut_rat)

        cx = np.random.randint(size=[B, ], low=int(W/8), high=W)
        cy = np.random.randint(size=[B, ], low=int(H/8), high=H)

        bbx1 = np.clip(cx - cut_w // 2, 0, W)
        bby1 = np.clip(cy - cut_h // 2, 0, H)

        bbx2 = np.clip(cx + cut_w // 2, 0, W)
        bby2 = np.clip(cy + cut_h // 2, 0, H)

        return bbx1, bby1, bbx2, bby2

    def cut_mix(self, labeled_image, labeled_mask,
                unlabeled_image=None, unlabeled_mask=None):
        mix_unlabeled_image = unlabeled_image.clone()
        mix_unlabeled_target = unlabeled_mask.clone()
        u_rand_index = torch.randperm(unlabeled_image.size()[0])[:unlabeled_image.size()[0]].cuda()
        u_bbx1, u_bby1, u_bbx2, u_bby2 = self.rand_bbox_1(unlabeled_image.size(), lam=np.random.beta(4, 4))

        for i in range(0, mix_unlabeled_image.shape[0]):
            mix_unlabeled_image[i, :, u_bbx1[i]:u_bbx2[i], u_bby1[i]:u_bby2[i]] = \
                unlabeled_image[u_rand_index[i], :, u_bbx1[i]:u_bbx2[i], u_bby1[i]:u_bby2[i]]

            mix_unlabeled_target[i, :, u_bbx1[i]:u_bbx2[i], u_bby1[i]:u_bby2[i]] = \
                unlabeled_mask[u_rand_index[i], :, u_bbx1[i]:u_bbx2[i], u_bby1[i]:u_bby2[i]]

        del unlabeled_image, unlabeled_mask

        return labeled_image, labeled_mask, mix_unlabeled_image, mix_unlabeled_target

    def predict_with_out_grad(self, image):
        with torch.no_grad():
            predict_target_ul1 = self.model.module.decoder1(self.model.module.encoder1(image),
                                                            data_shape=[image.shape[-2], image.shape[-1]])
            predict_target_ul2 = self.model.module.decoder2(self.model.module.encoder2(image),
                                                            data_shape=[image.shape[-2], image.shape[-1]])
            predict_target_ul1 = torch.nn.functional.interpolate(predict_target_ul1,
                                                                 size=(image.shape[-2], image.shape[-1]),
                                                                 mode='bilinear',
                                                                 align_corners=True)

            predict_target_ul2 = torch.nn.functional.interpolate(predict_target_ul2,
                                                                 size=(image.shape[-2], image.shape[-1]),
                                                                 mode='bilinear',
                                                                 align_corners=True)

            assert predict_target_ul1.shape == predict_target_ul2.shape, "Expect two prediction in same shape,"
        return predict_target_ul1, predict_target_ul2

    # NOTE: the func in here doesn't bring improvements, but stabilize the early stage's training curve.
    def assist_mask_calculate(self, core_predict, assist_predict, topk=1):
        _, index = torch.topk(assist_predict, k=topk, dim=1)
        mask = torch.nn.functional.one_hot(index.squeeze())
        # k!= 1, sum them
        mask = mask.sum(dim=1) if topk > 1 else mask
        if mask.shape[-1] != self.num_classes:
            mask = torch.cat((mask, torch.zeros([mask.shape[0], mask.shape[1],
                                                 mask.shape[2], self.num_classes - mask.shape[-1]]).cuda()),
                             dim=3)
        mask = mask.permute(0, 3, 1, 2)
        # get the topk result of the assist map
        assist_predict = torch.mul(assist_predict, mask)

        # fullfill with core predict value for the other entries;
        # as it will be merged based on threshold value
        assist_predict[torch.where(assist_predict == .0)] = core_predict[torch.where(assist_predict == .0)]
        return assist_predict

    def _warm_up(self, epoch, id):
        self.model.train()
        assert id == 1 or id == 2 or id == 3, "Expect ID in 1, 2 or 3"
        dataloader = iter(self.supervised_loader)
        tbar = range(len(self.supervised_loader))

        if self.args.ddp:
            self.supervised_loader.sampler.set_epoch(epoch=epoch-1)

        tbar = tqdm(tbar, ncols=135) if self.args.local_rank <= 0 else tbar
        self._reset_metrics()
        for batch_idx in tbar:
            (input_l_wk, input_l_str, target_l) = next(dataloader)

            # we only train the strong augmented student
            input_l = input_l_wk if id == 1 or id == 2 else input_l_str

            input_l, target_l = input_l.cuda(non_blocking=True), target_l.cuda(non_blocking=True)

            total_loss, cur_losses, outputs = self.model(x_l=input_l, target_l=target_l, x_ul=None,
                                                         target_ul=None, curr_iter=batch_idx,
                                                         epoch=epoch-1, id=id, warm_up=True)
            if id == 1:
                self.optimizer1.zero_grad()
            elif id == 2:
                self.optimizer2.zero_grad()
            else:
                self.optimizer_s.zero_grad()

            total_loss = total_loss.mean()
            total_loss.backward()
            if id == 1:
                self.optimizer1.step()
            elif id == 2:
                self.optimizer2.step()
            else:
                self.optimizer_s.step()

            self._update_losses(cur_losses)
            self._compute_metrics(outputs, target_l, None, sup=True)
            _ = self._log_values(cur_losses)

            del input_l, target_l
            del total_loss, cur_losses, outputs

            if self.args.local_rank <= 0:
                tbar.set_description('ID {} Warm ({}) | Ls {:.2f} |'.format(id, epoch, self.loss_sup.average))

        return

    def _pretrain(self, epoch, id):
        self.model.train()
        dataloader = iter(self.pretrain_loader)
        tbar = range(len(self.pretrain_loader))

        if self.args.ddp:
            self.pretrain_loader.sampler.set_epoch(epoch=epoch-1)


        tbar = range(len(self.unsupervised_loader))

        tbar = tqdm(tbar, ncols=135) if self.args.local_rank <= 0 else tbar
        self._reset_metrics()
        for batch_idx in tbar:
            image = next(dataloader)
            image = image.cuda(non_blocking=True)

            total_loss, cur_losses, outputs = self.model(x_l=image, target_l=image,
                                                         target_ul=None, curr_iter=batch_idx,
                                                         epoch=epoch-1, id=id, pretrained=True)

            self.optimizer_s2.zero_grad()

            total_loss = total_loss.mean()
            total_loss.backward()

            self.optimizer_s2.step()

            self._update_losses(cur_losses)
            _ = self._log_values(cur_losses)

            del image
            del total_loss, cur_losses, outputs

            if self.args.local_rank <= 0:
                tbar.set_description('Pretraining ({}) | Ls {:.2f} |'.format(epoch, self.loss_rec.average))

            self.lr_scheduler_s2.step(epoch=epoch-1)



        return

    def _train_epoch(self, epoch, id):
        assert id == 1 or id == 2, "Expect ID in 1 or 2"
        self.model.module.freeze_teachers_parameters()
        
        self.model.train()
        if self.args.ddp:
            self.supervised_loader.sampler.set_epoch(epoch=epoch-1)
            self.unsupervised_loader.sampler.set_epoch(epoch=epoch-1)
        dataloader = iter(zip(cycle(self.supervised_loader), self.unsupervised_loader))
        tbar = range(len(self.unsupervised_loader))

        tbar = tqdm(tbar, ncols=135) if self.args.local_rank <= 0 else tbar
        self._reset_metrics()
        for batch_idx in tbar:
            if self.args.local_rank <= 0:
                self.tensor_board.step_forward(len(self.unsupervised_loader)*(epoch-1)+batch_idx)

            if self.mode == "semi":
                (_, input_l, target_l), (input_ul_wk, input_ul_str, target_ul) = next(dataloader)
                input_ul_wk, input_ul_str, target_ul = input_ul_wk.cuda(non_blocking=True), \
                                                       input_ul_str.cuda(non_blocking=True), \
                                                       target_ul.cuda(non_blocking=True)
            else:
                (_, input_l, target_l) = next(dataloader)
                input_ul_wk, input_ul_str, target_ul = None, None, None
            
            # strong aug for all the supervised images
            input_l, target_l = input_l.cuda(non_blocking=True), target_l.cuda(non_blocking=True)

            # predicted unlabeled data
            if self.mode == "semi":
                t1_prob, t2_prob = self.predict_with_out_grad(input_ul_wk)
                # calculate the assistance result from other teacher
                if id == 1:
                    t2_prob = self.assist_mask_calculate(core_predict=t1_prob,
                                                         assist_predict=t2_prob,
                                                         topk=7)
                else:
                    t1_prob = self.assist_mask_calculate(core_predict=t2_prob,
                                                         assist_predict=t1_prob,
                                                         topk=7)
                predict_target_ul = self.gamma * t1_prob + (1 - self.gamma) * t2_prob
            else:
                predict_target_ul = None

            if self.args.ddp:
                dist.barrier()
            
            if batch_idx == 0 or batch_idx == int(len(self.unsupervised_loader)/2):
                if self.args.local_rank <= 0:
                    self.tensor_board.update_wandb_image(images=input_ul_wk,
                                                         ground_truth=target_ul,
                                                         teacher_prediction=predict_target_ul,
                                                         img_number=min(self.args.batch_size, 4))

                if self.args.ddp:
                    dist.barrier()

            origin_predict = predict_target_ul.detach().clone()

            if self.args.architecture == "psp" and id == 1:
                input_ul, predict_target_ul, _ = self.post_aug_process(input_ul_str,
                                                                       predict_target_ul, None)

            input_l, target_l, input_ul_str, predict_target_ul = self.cut_mix(input_l, target_l,
                                                                              input_ul_str,
                                                                              predict_target_ul)

            total_loss, cur_losses, outputs = self.model(x_l=input_l, target_l=target_l,
                                                         x_ul=input_ul_str,
                                                         target_ul=predict_target_ul,
                                                         curr_iter=batch_idx, epoch=epoch-1, id=id, 
                                                         semi_p_th=self.args.semi_p_th, 
                                                         semi_n_th=self.args.semi_n_th)


            # if batch_idx == 0 or batch_idx == int(len(self.unsupervised_loader)/2):
            #     if self.args.local_rank <= 0:
            #         self.tensor_board.update_wandb_recons_image(images=input_l,recon=outputs['rec'],img_number=min(self.args.batch_size, 4))
            #     if self.args.ddp:
            #         dist.barrier()

            total_loss = total_loss.mean()

            self.optimizer_s.zero_grad()
            total_loss.backward()

            self.optimizer_s.step()
            self._update_losses(cur_losses)
            outputs['unsup_pred'] = origin_predict
            self._compute_metrics(outputs, target_l, target_ul,
                                  sup=True if self.model.module.mode == "supervised" else False)

            _ = self._log_values(cur_losses)
            if self.args.local_rank <= 0:
                if batch_idx == 0 or batch_idx == int(len(self.unsupervised_loader)/2):
                    self.tensor_board.update_table(cur_losses['pass_rate']['entire_prob_boundary'],
                                                   axis_name={"x": "boundary", "y": "rate"},
                                                   title="pass_in_each_boundary")

                    self.tensor_board.update_table(cur_losses['pass_rate']['max_prob_boundary'],
                                                   axis_name={"x": "boundary", "y": "rate"},
                                                   title="max_prob_in_each_boundary")

                if batch_idx % self.log_step == 0:
                    for i, opt_group in enumerate(self.optimizer_s.param_groups[:2]):
                        self.tensor_board.upload_single_info({f"learning_rate_{i}": opt_group['lr']})
                    self.tensor_board.upload_single_info({"ramp_up": self.model.module.unsup_loss_w.current_rampup})
                tbar.set_description('ID {} T ({}) | Ls {:.3f} Lu {:.3f} Lw {:.3f} m1 {:.3f} m2 {:.3f}|'.format(
                    id, epoch, self.loss_sup.average, self.loss_unsup.average, self.loss_weakly.average,
                    self.mIoU_l, self.mIoU_ul))
            
            if self.args.ddp:
                dist.barrier()
            
            del input_l, target_l, input_ul_wk, input_ul_str, target_ul
            del total_loss, cur_losses, outputs

            self.lr_scheduler_s.step(epoch=epoch-1)

            with torch.no_grad():
                if id == 1:
                   self.update_teachers(teacher_encoder=self.model.module.encoder1,
                                        teacher_decoder=self.model.module.decoder1)
                else:
                   self.update_teachers(teacher_encoder=self.model.module.encoder2,
                                        teacher_decoder=self.model.module.decoder2)
                if self.args.ddp:
                    dist.barrier()

        return

    def _valid_epoch(self, epoch, id):
        assert self.val_loader is not None, "val loader error."
        self.logger.info('evaluating ...')
        self.model.eval()
        total_loss_val = AverageMeter()
        total_inter, total_union = 0, 0
        total_correct, total_label = 0, 0
        tbar = tqdm(self.val_loader, ncols=130)
        with torch.no_grad():
            for batch_idx, (data, target) in enumerate(tbar):
                target, data = target.cuda(non_blocking=True), data.cuda(non_blocking=True)
                H, W = target.size(1), target.size(2)
                up_sizes = (ceil(H / 8) * 8, ceil(W / 8) * 8)
                # resize to network acceptable
                data = torch.nn.functional.interpolate(data,
                                                       size=(up_sizes[0], up_sizes[1]),
                                                       mode='bilinear',
                                                       align_corners=True)

                output1, output2 = self.predict_with_out_grad(data)
                output = (output1 + output2)/2
                output = torch.nn.functional.interpolate(output,
                                                         size=(H, W),
                                                         mode='bilinear',
                                                         align_corners=True)

                # LOSS
                loss = F.cross_entropy(output, target, ignore_index=self.ignore_index)
                total_loss_val.update(loss.item())

                correct, labeled, inter, union = eval_metrics(output, target, self.num_classes, self.ignore_index)
                total_inter, total_union = total_inter + inter, total_union + union
                total_correct, total_label = total_correct + correct, total_label + labeled

                # PRINT INFO
                pixAcc = 1.0 * total_correct / (np.spacing(1) + total_label)
                IoU = 1.0 * total_inter / (np.spacing(1) + total_union)
                mIoU = IoU.mean().item()
                seg_metrics = {"Pixel_Accuracy": np.round(pixAcc, 4), "Mean_IoU": np.round(mIoU, 4),
                               "Class_IoU": dict(zip(range(self.num_classes), np.round(IoU, 4)))}

                tbar.set_description('EVAL ID ({}) ({}) | Loss: {:.4f}, PixelAcc: {:.4f}, Mean IoU: {:.4f} |'.format(
                    "Teachers",
                    epoch,
                    total_loss_val.average,
                    pixAcc,
                    mIoU))

            valid_dict = {}
            for k, v in list(seg_metrics.items())[:-1]:
                valid_dict[f'valid_{k}'] = v
            self.tensor_board.upload_wandb_info(valid_dict)

            log = {
                'val_loss': total_loss_val.average,
                **seg_metrics
            }
        return log

    def _reset_metrics(self):
        self.loss_sup = AverageMeter()
        self.loss_rec_sup = AverageMeter()
        self.loss_unsup = AverageMeter()
        self.loss_rec_unsup = AverageMeter()
        self.loss_rec = AverageMeter()
        self.loss_weakly = AverageMeter()
        self.pair_wise = AverageMeter()
        self.total_inter_l, self.total_union_l = 0, 0
        self.total_correct_l, self.total_label_l = 0, 0
        self.total_inter_ul, self.total_union_ul = 0, 0
        self.total_correct_ul, self.total_label_ul = 0, 0
        self.mIoU_l, self.mIoU_ul = 0, 0
        self.pixel_acc_l, self.pixel_acc_ul = 0, 0
        self.class_iou_l, self.class_iou_ul = {}, {}

    def _update_losses(self, cur_losses):
        if "loss_sup" in cur_losses.keys():
            self.loss_sup.update(cur_losses['loss_sup'].mean().item())
        if "loss_rec_sup" in cur_losses.keys():
            self.loss_rec_sup.update(cur_losses['loss_rec_sup'].mean().item())
        if "loss_unsup" in cur_losses.keys():
            self.loss_unsup.update(cur_losses['loss_unsup'].mean().item())
        if "loss_rec_unsup" in cur_losses.keys():
            self.loss_rec_unsup.update(cur_losses['loss_rec_unsup'].mean().item())
        if "loss_rec" in cur_losses.keys():
            self.loss_rec.update(cur_losses['loss_rec'].mean().item())
        if "loss_weakly" in cur_losses.keys():
            self.loss_weakly.update(cur_losses['loss_weakly'].mean().item())
        if "pair_wise" in cur_losses.keys():
            self.pair_wise.update(cur_losses['pair_wise'].mean().item())

    def _compute_metrics(self, outputs, target_l, target_ul, sup=False):
        seg_metrics_l = eval_metrics(outputs['sup_pred'], target_l, self.num_classes, self.ignore_index)
        self._update_seg_metrics(*seg_metrics_l, True)
        seg_metrics_l = self._get_seg_metrics(True)
        self.pixel_acc_l, self.mIoU_l, self.class_iou_l = seg_metrics_l.values()

        if sup:
            return

        if self.mode == 'semi':
            seg_metrics_ul = eval_metrics(outputs['unsup_pred'], target_ul, self.num_classes, self.ignore_index)
            self._update_seg_metrics(*seg_metrics_ul, False)
            seg_metrics_ul = self._get_seg_metrics(False)
            self.pixel_acc_ul, self.mIoU_ul, self.class_iou_ul = seg_metrics_ul.values()

    def _update_seg_metrics(self, correct, labeled, inter, union, supervised=True):
        if supervised:
            self.total_correct_l += correct
            self.total_label_l += labeled
            self.total_inter_l += inter
            self.total_union_l += union
        else:
            self.total_correct_ul += correct
            self.total_label_ul += labeled
            self.total_inter_ul += inter
            self.total_union_ul += union

    def _get_seg_metrics(self, supervised=True):
        if supervised:
            pixAcc = 1.0 * self.total_correct_l / (np.spacing(1) + self.total_label_l)
            IoU = 1.0 * self.total_inter_l / (np.spacing(1) + self.total_union_l)
        else:
            pixAcc = 1.0 * self.total_correct_ul / (np.spacing(1) + self.total_label_ul)
            IoU = 1.0 * self.total_inter_ul / (np.spacing(1) + self.total_union_ul)
        mIoU = IoU.mean()
        return {
            "Pixel_Accuracy": np.round(pixAcc, 3),
            "Mean_IoU": np.round(mIoU, 3),
            "Class_IoU": dict(zip(range(self.num_classes), np.round(IoU, 3)))
        }

    def _log_values(self, cur_losses):
        logs = {}
        if "loss_sup" in cur_losses.keys():
            logs['loss_sup'] = self.loss_sup.average

        if "loss_rec_sup" in cur_losses.keys():
            logs['loss_rec_sup'] = self.loss_rec_sup.average

        if "loss_unsup" in cur_losses.keys():
            logs['loss_unsup'] = self.loss_unsup.average

        if "loss_rec_unsup" in cur_losses.keys():
            logs['loss_rec_unsup'] = self.loss_unsup.average

        if "loss_rec" in cur_losses.keys():
            logs['loss_rec'] = self.loss_rec.average

        if "loss_weakly" in cur_losses.keys():
            logs['loss_weakly'] = self.loss_weakly.average
        if "pair_wise" in cur_losses.keys():
            logs['pair_wise'] = self.pair_wise.average

        logs['mIoU_labeled'] = self.mIoU_l
        logs['pixel_acc_labeled'] = self.pixel_acc_l

        if self.args.local_rank <= 0:
            self.tensor_board.upload_single_info({'loss_sup': self.loss_sup.average})
            self.tensor_board.upload_single_info({'loss_rec': self.loss_rec.average})
            self.tensor_board.upload_single_info({'loss_rec_sup': self.loss_rec_sup.average})
            self.tensor_board.upload_single_info({'mIoU_labeled': self.mIoU_l})
            self.tensor_board.upload_single_info({'pixel_acc_labeled': self.pixel_acc_l})

            if self.mode == 'semi':
                logs['mIoU_unlabeled'] = self.mIoU_ul
                logs['pixel_acc_unlabeled'] = self.pixel_acc_ul
                self.tensor_board.upload_single_info({'loss_unsup': self.loss_unsup.average})
                self.tensor_board.upload_single_info({'loss_rec_unsup': self.loss_rec_unsup.average})
                self.tensor_board.upload_single_info({'mIoU_unlabeled':  self.mIoU_ul})
                self.tensor_board.upload_single_info({'pixel_acc_unlabeled': self.pixel_acc_ul})

        return logs


