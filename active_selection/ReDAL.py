# basic
import os
import json
import numpy as np
import pandas as pd
from tqdm import tqdm
# torch
import torch
import torch.distributed as dist
from torch_scatter import scatter_mean
# custom
from active_selection.utils import get_al_loader
from active_selection.diversity import importance_reweight


class ReDALSelector:

    def __init__(self, batch_size, num_workers, config_path):
        self.batch_size = batch_size
        self.num_workers = num_workers
        if config_path is None:
            raise ValueError("Please specify ReDAL config path when using ReDAL active strategy.")
        with open(config_path, "r") as f:
            self.config = f.read(config_path)

    def calculate_scores(self, trainer, pool_set):
        pool_set.entropy_only = False
        model = trainer.net
        model.eval()

        loader, idx = get_al_loader(trainer, pool_set, self.batch_size, self.num_workers)
        print(idx)
        all_feats = np.zeros((0, 96))
        scores = []
        tqdm_loader = tqdm(loader, total=len(loader))
        do_not_convert = ['file_name', 'curvature', 'colorgrad']
        with torch.no_grad():
            for i_iter_test, batch in enumerate(tqdm_loader):
                # predict
                for key, value in batch.items():
                    if key not in do_not_convert:
                        batch[key] = value.cuda()

                inputs = batch['lidar']
                outputs = model(inputs)
                preds = outputs['final']
                invs = batch['inverse_map']
                all_labels = batch['targets_mapped']

                feats = outputs['pt_feat']
                featC = feats.C.cpu().numpy()
                supvox = batch['targets'].F.long()

                scene_pts = inputs.C.cpu().numpy()
                invsC = invs.C.cpu().numpy()
                invsF = invs.F.cpu().numpy()

                all_labels_F = all_labels.F.cpu().numpy()
                all_labels_C = all_labels.C.cpu().numpy()

                for batch_idx in range(self.batch_size):
                    fname = batch['file_name'][batch_idx]
                    colorgrad = batch['colorgrad'][batch_idx]
                    curvature = batch['curvature'][batch_idx]
                    assert fname == pool_set.im_idx[idx]

                    cur_scene_pts = (scene_pts[:, -1] == batch_idx)
                    cur_inv = invsF[invsC[:, -1] == batch_idx]
                    output = preds[cur_scene_pts][cur_inv]
                    output = torch.nn.functional.softmax(output, dim=1)
                    uncertain = torch.mean(-output * torch.log2(output + 1e-12), dim=1)
                    uncertain = uncertain.cpu().detach().numpy()
                    cur_label = (all_labels_C[:, -1] == batch_idx)
                    cur_supvox = all_labels_F[cur_label]

                    # region feature
                    feat = feats.F[featC[:, -1] == batch_idx]
                    supvox_id = supvox[featC[:, -1] == batch_idx]
                    feat = scatter_mean(feat, supvox_id, dim=0).cpu().numpy()

                    key = pool_set.im_idx[idx]
                    selected_row = np.array(pool_set.supvox[key])
                    feat = feat[selected_row]
                    all_feats = np.concatenate([all_feats, feat], axis=0)
                    # Groupby
                    val = self.config.alpha * uncertain + self.config.beta * colorgrad + self.config.gamma * curvature
                    df = pd.DataFrame({'id': cur_supvox, 'val': val})
                    df1 = df.groupby('id')['val'].agg(['count', 'mean']).reset_index()
                    table = df1[df1['id'].isin(pool_set.supvox[key])].drop(columns=['count'])
                    table['key'] = key
                    table = table.reindex(columns=['mean', 'key', 'id'])
                    region_score = list(table.itertuples(index=False, name=None))
                    scores.extend(region_score)

                    idx += 1
                    if idx >= len(pool_set.im_idx):
                        break
                if idx >= len(pool_set.im_idx):
                    break
        # save region entropy & feature
        fname = os.path.join(trainer.model_save_dir, "AL_record", f"region_val_{trainer.local_rank}.json")
        with open(fname, "w") as f:
            json.dump(scores, f)
        npy_fname = os.path.join(trainer.model_save_dir, "AL_record", f"region_feat_{trainer.local_rank}.npy")
        np.save(npy_fname, all_feats)

    def select_next_batch(self, trainer, active_set, selection_percent):
        self.calculate_scores(trainer, active_set.pool_dataset)
        if trainer.distributed is False:
            # load uncertainty
            fname = os.path.join(trainer.model_save_dir, "AL_record", "region_val_0.json")
            with open(fname, "r") as f:
                scores = json.load(f)
            # load region feature
            feat_fname = os.path.join(trainer.model_save_dir, "AL_record", "region_feat_0.npy")
            features = np.load(feat_fname)

            # selected_samples = importance_reweight(scores, features)
            selected_samples = importance_reweight(scores, features, self.config)
            active_set.expand_training_set(selected_samples, selection_percent)
        else:
            dist.barrier()
            if trainer.local_rank == 0:
                # load uncertainty
                scores = []
                for i in range(dist.get_world_size()):
                    fname = os.path.join(trainer.model_save_dir, "AL_record", f"region_val_{i}.json")
                    with open(fname, "r") as f:
                        scores.extend(json.load(f))
                # load region feature
                feat_lst = []
                for i in range(dist.get_world_size()):
                    npy_fname = os.path.join(trainer.model_save_dir, "AL_record", f"region_feat_{i}.npy")
                    feat_lst.append(np.load(npy_fname))
                features = np.concatenate(feat_lst, 0)

                selected_samples = importance_reweight(scores, features, self.config)
                active_set.expand_training_set(selected_samples, selection_percent)
