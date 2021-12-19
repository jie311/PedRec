import sys
from pathlib import Path

from pedrec.networks.net_pedrec.pedrec_net import PedRecNet
from pedrec.training.experiments.experiment_initializer import initialize_weights_with_same_name_and_shape
from pedrec.training.experiments.experiment_path_helper import get_experiment_paths_home

sys.path.append(".")

from torch.utils.data import DataLoader

from pedrec.configs.dataset_configs import PedRecDatasetConfig, get_sim_val_dataset_cfg_default, \
    get_h36m_dataset_cfg_default, get_sim_dataset_cfg_default
from pedrec.datasets.pedrec_dataset import PedRecDataset
from pedrec.evaluations.eval_helper import get_total_coords
from pedrec.models.constants.dataset_constants import DatasetType
import torch
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
from pedrec.configs.pedrec_net_config import PedRecNet50Config
from pedrec.training.experiments.experiment_train_helper import init_experiment, \
    get_outputs_loss_mtl
from pedrec.utils.torch_utils.torch_helper import get_device, move_to_device
import pandas as pd
import numpy as np
from pedrec.models.constants.skeleton_pedrec import SKELETON_PEDREC_JOINTS


def set_df_dtypes(df: pd.DataFrame):
    df["index"] = df["index"].astype("int")

    for joint in SKELETON_PEDREC_JOINTS:
        df[f"skeleton2d_{joint.name}_x"] = df[f"skeleton2d_{joint.name}_x"].astype("float32")
        df[f"skeleton2d_{joint.name}_y"] = df[f"skeleton2d_{joint.name}_y"].astype("float32")
        df[f"skeleton2d_{joint.name}_score"] = df[f"skeleton2d_{joint.name}_score"].astype("float32")
        df[f"skeleton2d_{joint.name}_visible"] = df[f"skeleton2d_{joint.name}_visible"].astype("category")
        df[f"skeleton2d_{joint.name}_supported"] = df[f"skeleton2d_{joint.name}_visible"].astype("category")

        df[f"skeleton3d_{joint.name}_x"] = df[f"skeleton3d_{joint.name}_x"].astype("float32")
        df[f"skeleton3d_{joint.name}_y"] = df[f"skeleton3d_{joint.name}_y"].astype("float32")
        df[f"skeleton3d_{joint.name}_z"] = df[f"skeleton3d_{joint.name}_z"].astype("float32")
        df[f"skeleton3d_{joint.name}_score"] = df[f"skeleton3d_{joint.name}_score"].astype("float32")
        df[f"skeleton3d_{joint.name}_visible"] = df[f"skeleton3d_{joint.name}_visible"].astype("category")
        df[f"skeleton3d_{joint.name}_supported"] = df[f"skeleton3d_{joint.name}_visible"].astype("category")

    df["body_orientation_phi"] = df["body_orientation_phi"].astype("float32")
    df["body_orientation_theta"] = df["body_orientation_theta"].astype("float32")
    df["body_orientation_score"] = df["body_orientation_score"].astype("float32")
    df["body_orientation_theta_supported"] = df["body_orientation_theta_supported"].astype("category")
    df["body_orientation_phi_supported"] = df["body_orientation_phi_supported"].astype("category")

    df["head_orientation_phi"] = df["head_orientation_phi"].astype("float32")
    df["head_orientation_theta"] = df["head_orientation_theta"].astype("float32")
    df["head_orientation_score"] = df["head_orientation_score"].astype("float32")
    df["head_orientation_theta_supported"] = df["head_orientation_theta_supported"].astype("category")
    df["head_orientation_phi_supported"] = df["head_orientation_phi_supported"].astype("category")


def get_column_names():
    column_names = [
        "index"
    ]
    for joint in SKELETON_PEDREC_JOINTS:
        column_names.append(f"skeleton2d_{joint.name}_x")
        column_names.append(f"skeleton2d_{joint.name}_y")
        column_names.append(f"skeleton2d_{joint.name}_score")
        column_names.append(f"skeleton2d_{joint.name}_visible")
        column_names.append(f"skeleton2d_{joint.name}_supported")
    for joint in SKELETON_PEDREC_JOINTS:
        column_names.append(f"skeleton3d_{joint.name}_x")
        column_names.append(f"skeleton3d_{joint.name}_y")
        column_names.append(f"skeleton3d_{joint.name}_z")
        column_names.append(f"skeleton3d_{joint.name}_score")
        column_names.append(f"skeleton3d_{joint.name}_visible")
        column_names.append(f"skeleton3d_{joint.name}_supported")
        
    column_names.append("body_orientation_theta")
    column_names.append("body_orientation_phi")
    column_names.append("body_orientation_score")
    column_names.append("body_orientation_theta_supported")
    column_names.append("body_orientation_phi_supported")

    column_names.append("head_orientation_theta")
    column_names.append("head_orientation_phi")
    column_names.append("head_orientation_score")
    column_names.append("head_orientation_theta_supported")
    column_names.append("head_orientation_phi_supported")

    return column_names


def get_preds_mtl(outputs: torch.Tensor):
    return {
        "skeleton": outputs[0].cpu().detach().numpy(),
        "skeleton_3d": outputs[1].cpu().detach().numpy(),
        "orientation": outputs[2].cpu().detach().numpy(),
    }


def main(output_path: str, dataset_cfg: PedRecDatasetConfig, pedrec_dataset_dir, pedrec_dataset_filename, weights_path: str, flip_all: bool = False):
    # experiment_paths = get_experiment_paths_home()
    net_cfg = PedRecNet50Config()
    # sim_val_dataset_cfg: PedRecDatasetConfig = get_sim_val_dataset_cfg_default()
    # sim_val_dataset_cfg.subsample = 1
    init_experiment(42)
    device = get_device(use_gpu=True)

    ####################################################################################################################
    ############################################ Initialize Network ####################################################
    ####################################################################################################################
    net = PedRecNet(net_cfg)
    net.init_weights()
    initialize_weights_with_same_name_and_shape(net, weights_path, "model.")
    net.to(device)

    ####################################################################################################################
    ################################################# Datasets #########################################################
    ####################################################################################################################
    trans = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    dataset = PedRecDataset(pedrec_dataset_dir,
                            pedrec_dataset_filename,
                            DatasetType.VALIDATE,
                            dataset_cfg,
                            net_cfg.model.input_size,
                            trans,
                            flip_all=flip_all)

    batch_size = 48
    data_loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=12)
    net.eval()
    result_rows = np.ndarray((len(dataset), len(get_column_names())))
    gt_rows = []
    count = 0
    with torch.no_grad():
        for test_data in data_loader:
            # if count > 2:
            #     break
            images, labels = test_data
            images = images.to(device)
            labels = move_to_device(labels, device)
            outputs = net(images)
            preds = get_preds_mtl(outputs)

            idxs = labels["idx"].cpu().detach().numpy()
            centers = labels["center"].cpu().detach().numpy()
            scales = labels["scale"].cpu().detach().numpy()
            rotations = labels["rotation"].cpu().detach().numpy()

            pose2d_preds = preds["skeleton"]
            pose3d_preds = preds["skeleton_3d"]
            orientation_preds = preds["orientation"]
            pose2d_preds = get_total_coords(pose2d_preds, net_cfg.model.input_size, centers, scales, rotations)
            pose3d_preds[:, :, :3] = (pose3d_preds[:, :, :3] * 3000) - 1500  # to cm

            pose2d_gts = labels["skeleton"].cpu().detach().numpy()
            pose3d_gts = labels["skeleton_3d"].cpu().detach().numpy()
            orientation_gts = labels["orientation"].cpu().detach().numpy()
            pose2d_gts = get_total_coords(pose2d_gts, net_cfg.model.input_size, centers, scales, rotations)
            pose3d_gts[:, :, :3] = (pose3d_gts[:, :, :3] * 3000) - 1500  # to cm


            for i in range(0, pose2d_preds.shape[0]):
                idx = np.array(idxs[i])
                pose2d_pred = pose2d_preds[i]
                pose3d_pred = pose3d_preds[i]
                orientation_pred = orientation_preds[i]
                pose2d_gt = pose2d_gts[i]
                pose3d_gt = pose3d_gts[i]
                orientation_gt = orientation_gts[i]
                visibles = pose2d_gt[:, 3]
                supported = pose2d_gt[:, 4]
                visible_supported = np.array([visibles, supported]).transpose(1, 0)
                pose2d_pred = np.concatenate((pose2d_pred, visible_supported), axis=1)
                pose3d_pred = np.concatenate((pose3d_pred, visible_supported), axis=1)
                pose2d_pred = pose2d_pred.reshape(-1)
                pose3d_pred = pose3d_pred.reshape(-1)
                orientation_pred_body = orientation_pred[0].reshape(-1)
                orientation_pred_head = orientation_pred[1].reshape(-1)

                pose2d_gt = pose2d_gt.reshape(-1)
                pose3d_gt = pose3d_gt.reshape(-1)
                orientation_gt_body = orientation_gt[0].reshape(-1)
                orientation_gt_head = orientation_gt[1].reshape(-1)
                result_row = np.concatenate([[idx], pose2d_pred, pose3d_pred, orientation_pred_body, orientation_gt_body[2:], orientation_pred_head, orientation_gt_head[2:]])
                result_rows[count] = np.array(result_row, dtype=np.float32)
                gt_row = np.concatenate([[idx], pose2d_gt, pose3d_gt, orientation_gt_body, orientation_gt_head])
                gt_rows.append(gt_row)
                count += 1
    print("finished")
    df = pd.DataFrame(data=result_rows, columns=get_column_names())
    set_df_dtypes(df)
    df.to_pickle(output_path)
    print("saved pred")
    del result_rows
    del df

    df = pd.DataFrame(data=gt_rows, columns=get_column_names())
    set_df_dtypes(df)
    df.to_pickle(output_path.replace("pred", "gt"))
    print("saved gt")

if __name__ == '__main__':
    # pedrec_dataset_dir = "data/datasets/Human3.6m/val/"
    # pedrec_dataset_filename = "h36m_val_pedrec.pkl"
    # pedrec_dataset_output_filename = "data/datasets/Human3.6m/val/h36m_val_v5_pose_results.pkl"
    # cfg = get_h36m_dataset_cfg_default()
    # cfg.subsample = 1

    # pedrec_dataset_dir = "data/datasets/Conti01/"
    # pedrec_dataset_filename = "rt_conti_01_train.pkl"
    # pedrec_dataset_output_filename = "data/datasets/Conti01/RESULTS-SIM-C01-TRAIN_pedrec_p2d3d_c_o_h36m_sim_mebow_0.pkl"
    # cfg = get_sim_dataset_cfg_default()
    # cfg.subsample = 1
    # main(pedrec_dataset_output_filename, cfg, pedrec_dataset_dir, pedrec_dataset_filename)

    experiment_paths = get_experiment_paths_home()
    network_paths = [
        # experiment_paths.pose_2d_coco_only_weights_path,
        # experiment_paths.pedrec_2d_h36m_path,
        # experiment_paths.pedrec_2d_sim_path,
        # experiment_paths.pedrec_2d3d_h36m_path,
        # experiment_paths.pedrec_2d3d_sim_path,
        # experiment_paths.pedrec_2d3d_h36m_sim_path,
        # experiment_paths.pedrec_2d_c_path,
        # experiment_paths.pedrec_2d3d_c_h36m_path,
        # experiment_paths.pedrec_2d3d_c_sim_path,
        # experiment_paths.pedrec_2d3d_c_h36m_sim_path,
        # experiment_paths.pedrec_2d3d_c_o_h36m_mebow_path,
        # experiment_paths.pedrec_2d3d_c_o_sim_path,
        # experiment_paths.pedrec_2d3d_c_o_h36m_sim_path,
        experiment_paths.pedrec_2d3d_c_o_h36m_sim_mebow_path,
    ]
    pedrec_dataset_dir = "data/datasets/Conti01/"
    pedrec_dataset_filename = "rt_conti_01_train_FIN.pkl"
    cfg = get_sim_dataset_cfg_default()
    cfg.subsample = 1
    for net_path in network_paths:
        experiment_name = Path(net_path).stem
        pedrec_dataset_output_filename = f"data/datasets/Conti01/results/C01F_train_pred_df_{experiment_name}.pkl"
        main(pedrec_dataset_output_filename, cfg, pedrec_dataset_dir, pedrec_dataset_filename, net_path, False)

        # pedrec_dataset_output_filename = f"data/datasets/Conti01/results/C01F_train_pred_df_{experiment_name}_flipped.pkl"
        # main(pedrec_dataset_output_filename, cfg, pedrec_dataset_dir, pedrec_dataset_filename, net_path, True)



