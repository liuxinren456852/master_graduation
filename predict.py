import argparse
import os
import json
import numpy as np
import open3d
import time
import torch
from data.semantic_dataset import SemanticDataset
from data.npm_dataset import NpmDataset
from utils.metric import ConfusionMatrix
from utils.model_util import select_model, run_model
from utils.eval_utils import _2common
from utils.point_cloud_util import _label_to_colors_by_name
from tensorboardX import SummaryWriter


# Parser
parser = argparse.ArgumentParser()
parser.add_argument('--gpu_id', type=int, default=1, help='gpu id for network')
parser.add_argument("--num_samples", type=int, default=500, help="# samples, each contains num_point points_centered")
parser.add_argument("--resume_model", default="/home/yss/sda1/yzl/yzl_graduation/train_log/PBC_pure_semantic_row40/checkpoint_epoch15_iou0.68.tar", help="restore checkpoint file storing model parameters")
parser.add_argument("--config_file", default="semantic.json",
                    help="config file path, it should same with that during traing")
parser.add_argument("--set", default="validation", help="train, validation, test")
parser.add_argument('--num_point', help='downsample number before feed to net', type=int, default=8192)
parser.add_argument('--model_name', '-m', help='Model to use', required=True)
parser.add_argument('--batch_size', type=int, default=16,
                    help='Batch Size for prediction [default: 32]')
parser.add_argument('--from_dataset', default='semantic', help='which dataset the model is trained from')
parser.add_argument('--to_dataset', default='semantic', help='which dataset to predict')
parser.add_argument('--embedding', default=False, action='store_true')
flags = parser.parse_args()
print(flags)

if __name__ == "__main__":
    np.random.seed(0)
    os.environ["CUDA_VISIBLE_DEVICES"] = str(flags.gpu_id)
    hyper_params = json.loads(open(flags.config_file).read())

    # Create output dir
    sub_folder = flags.model_name + '_' + flags.from_dataset + '2' + flags.to_dataset + '_' + flags.set
    output_dir = os.path.join("result", "sparse", sub_folder)
    os.makedirs(output_dir, exist_ok=True)

    # Dataset
    if flags.to_dataset == 'semantic':
        dataset = SemanticDataset(
            num_points_per_sample=flags.num_point,
            split=flags.set,
            box_size_x=hyper_params["box_size_x"],
            box_size_y=hyper_params["box_size_y"],
            use_color=hyper_params["use_color"],
            use_geometry=hyper_params['use_geometry'],
            path=hyper_params["data_path"],
        )
    elif flags.to_dataset == 'npm':
        dataset = NpmDataset(
            num_points_per_sample=flags.num_point,
            split=flags.set,
            box_size_x=hyper_params["box_size_x"],
            box_size_y=hyper_params["box_size_y"],
            use_geometry=hyper_params['use_geometry'],
            path=hyper_params["data_path"],
        )
    else:
        print("dataset error")
        raise ValueError

    # Model
    if torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        raise ValueError("GPU not found!")
    batch_size = flags.batch_size
    if flags.from_dataset == 'semantic':
        classes_in_model = 9
    else:
        classes_in_model = 10
    # load model
    resume_path = flags.resume_model
    model = select_model(flags.model_name, classes_in_model, hyper_params)[0]
    model = model.to(device)
    print("Resuming From ", resume_path)
    checkpoint = torch.load(resume_path)
    saved_state_dict = checkpoint['state_dict']
    model.load_state_dict(saved_state_dict)

    # Process each file
    cm = ConfusionMatrix(classes_in_model)
    common_cm = ConfusionMatrix(6)
    model = model.eval()
    # for visulization in tensorboard
    writer = SummaryWriter('runs/embedding_example')
    global_step = 0
    for file_data in dataset.list_file_data:
        print("Processing {}".format(file_data.file_path_without_ext))

        # Predict for num_samples times
        points_collector = []
        pd_labels_collector = []
        pd_prob_collector = []
        pd_common_labels_collector = []

        # If flags.num_samples < batch_size, will predict one batch
        for batch_index in range(int(np.ceil(flags.num_samples / batch_size))):
            global_step += 1
            current_batch_size = min(batch_size, flags.num_samples - batch_index * batch_size)

            # Get data
            if flags.to_dataset == 'semantic':
                points_centered, points, gt_labels, colors, geometry = file_data.sample_batch(
                    batch_size=current_batch_size,
                    num_points_per_sample=flags.num_point,
                )
            else:
                points_centered, points, gt_labels, geometry = file_data.sample_batch(
                    batch_size=current_batch_size,
                    num_points_per_sample=flags.num_point,
                )

            data_list = [points_centered]
            # only semantic3d dataset can set 'use_color' 1
            if hyper_params['use_color']:
                data_list.append(colors)
            if hyper_params['use_geometry']:
                data_list.append(geometry)
            point_cloud = np.concatenate(data_list, axis=-1)

            # Predict
            s = time.time()
            input_tensor = torch.from_numpy(point_cloud).to(device, dtype=torch.float32)  # (current_batch_size, N, 3)
            with torch.no_grad():
                res = run_model(model, input_tensor, hyper_params, flags.model_name, return_embed=True)  # (current_batch_size, N)
            if flags.model_name in ['pointsemantic', 'pointsemantic_dense', 'PBC_pure']:
                pd_prob, embedding = res
            elif flags.model_name in ['pointsemantic_folding', 'pointsemantic_atlas', 'pointsemantic_caps',
                                      'PBC_folding', 'PBC_atlas', 'PBC_caps']:
                pd_prob, reconstruction = res
            else:
                pd_prob = res
            _, pd_labels = torch.max(pd_prob, dim=2)  # (B, N)
            pd_prob = pd_prob.cpu().numpy()
            pd_labels = pd_labels.cpu().numpy()
            if flags.model_name == 'pointsemantic' and flags.embedding:
                embedding = embedding.cpu().numpy()
                reshaped_embedding = np.reshape(embedding, (flags.batch_size*flags.num_point, -1))
                if global_step < 5:
                    writer.add_embedding(
                        reshaped_embedding,
                        metadata=gt_labels.flatten(),
                        global_step=global_step
                    )
            print("Batch size: {}, time: {}".format(current_batch_size, time.time() - s))

            common_gt = _2common(gt_labels, flags.to_dataset)  # (B, N)
            common_pd = _2common(pd_labels, flags.from_dataset)  # (B, N)

            # Save to collector for file output
            points_collector.extend(points)  # (B, N, 3)
            pd_labels_collector.extend(pd_labels)  # (B, N)
            pd_common_labels_collector.extend(common_pd)  # (B, N)
            pd_prob_collector.extend(pd_prob)  # (B, N, num_classes)

            # Increment confusion matrix

            common_cm.increment_from_list(common_gt.flatten(), common_pd.flatten())
            if flags.from_dataset == flags.to_dataset:
                cm.increment_from_list(gt_labels.flatten(), pd_labels.flatten())

        # Save sparse point cloud and predicted labels
        file_prefix = os.path.basename(file_data.file_path_without_ext)

        sparse_points = np.array(points_collector).reshape((-1, 3))  # (B*N, 3)
        sparse_common_labels = np.array(pd_common_labels_collector).flatten()  # (B*N,)
        pcd_common = open3d.geometry.PointCloud()
        pcd_common.points = open3d.utility.Vector3dVector(sparse_points)
        pcd_common.colors = open3d.utility.Vector3dVector(_label_to_colors_by_name(sparse_common_labels, 'common'))
        pcd_path = os.path.join(output_dir, file_prefix + "_common.pcd")
        open3d.io.write_point_cloud(pcd_path, pcd_common)
        print("Exported sparse common pcd to {}".format(pcd_path))

        pd_labels_path = os.path.join(output_dir, file_prefix + "_common.labels")
        np.savetxt(pd_labels_path, sparse_common_labels, fmt="%d")
        print("Exported sparse common labels to {}".format(pd_labels_path))

        sparse_prob = np.array(pd_prob_collector).astype(float).reshape(-1, classes_in_model)  # (B*N, num_classes)
        pd_probs_path = os.path.join(output_dir, file_prefix + ".prob")
        np.savetxt(pd_probs_path, sparse_prob, fmt="%f")
        print("Exported sparse probs to {}".format(pd_probs_path))

        # save original labels and visulize them with from_dataset labels
        sparse_labels = np.array(pd_labels_collector).astype(int).flatten()  # (B*N,)
        pcd_ori = open3d.geometry.PointCloud()
        pcd_ori.points = open3d.utility.Vector3dVector(sparse_points)
        pcd_ori.colors = open3d.utility.Vector3dVector(_label_to_colors_by_name(sparse_labels, flags.from_dataset))
        pcd_ori_path = os.path.join(output_dir, file_prefix + '_' + flags.from_dataset + ".pcd")
        open3d.io.write_point_cloud(pcd_ori_path, pcd_ori)
        print("Exported sparse pcd to {}".format(pcd_ori_path))

        pd_ori_labels_path = os.path.join(output_dir, file_prefix + '_' + flags.from_dataset + ".labels")
        np.savetxt(pd_ori_labels_path, sparse_labels, fmt="%d")
        print("Exported sparse labels to {}".format(pd_ori_labels_path))

    print("the following is the result of common class:")
    common_cm.print_metrics()
    print("#" * 100)
    if flags.from_dataset == flags.to_dataset:
        print("the following is the result of original class:")
        cm.print_metrics()
