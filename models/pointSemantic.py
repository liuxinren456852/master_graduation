import torch.nn as nn
import torch
import torch.nn.parallel
import torch.nn.functional as F
from utils.pointnet_util import PNSADenseNet_GAT, PNFPDenseNet
from utils.folding_utils import FoldingNetDec


class PointSemantic(nn.Module):
    def __init__(self, num_classes, addition_dim=3):
        super(PointSemantic, self).__init__()
        # Encoder-GAT
        self.sa1 = PNSADenseNet_GAT(1024, 0.5, 64, [addition_dim + 3, 32, 64], 0.6, 0.2, False, False)
        self.sa2 = PNSADenseNet_GAT(256, 1, 48, [64 + 3, 64, 128], 0.6, 0.2, False, False)
        self.sa3 = PNSADenseNet_GAT(64, 2, 32, [128 + 3, 128, 256], 0.6, 0.2, False, False)
        self.sa4 = PNSADenseNet_GAT(16, 4, 16, [256 + 3, 256, 512], 0.6, 0.2, False, False)

        # Encoder-Dual attention
        # self.sa1 = PNSADenseNet_Dual(1024, 0.5, 64, [3 + 3, 32, 64], False, True, False)
        # self.sa2 = PNSADenseNet_Dual(256, 1, 48, [64 + 3, 64, 128], False, True, False)
        # self.sa3 = PNSADenseNet_Dual(64, 2, 32, [128 + 3, 128, 256], False, True, False)
        # self.sa4 = PNSADenseNet_Dual(16, 4, 16, [256 + 3, 256, 512], False, True, False)

        # Decoder-semantic3d
        self.fp4 = PNFPDenseNet([768, 256, 256])
        self.fp3 = PNFPDenseNet([384, 256, 256])
        self.fp2 = PNFPDenseNet([320, 256, 128])
        self.fp1 = PNFPDenseNet([128, 128, 128])
        self.conv1 = nn.Conv1d(128, 128, 1)
        self.bn1 = nn.BatchNorm1d(128)
        self.drop1 = nn.Dropout(0.3)
        self.conv2 = nn.Conv1d(128, num_classes, 1)

        # Decoder-kitti
        self.fold = FoldingNetDec(65)

        self.addition_dim = addition_dim

    def forward(self, pointcloud1, pointcloud2):
        """
        Input:
             pointcloud1: input tensor anchor (B, N, 6) semantic_3d
             pointcloud2: input tensor anchor (B, N, 6) npm3d
        return:
             lp: absolute position (2B, 3)
             lx: relative translation (B, 3)
        """
        # input point cloud
        assert(pointcloud1.shape == pointcloud2.shape)
        B, N, channel = pointcloud1.shape
        add_dim = channel - 3
        assert(add_dim == self.addition_dim)
        l0_xyz1 = pointcloud1[:, :, :3]
        l0_xyz2 = pointcloud2[:, :, :3]
        l0_points1 = pointcloud1[:, :, 3:]
        l0_points2 = pointcloud2[:, :, 3:]

        # semantic3d encoder (share weighted)
        l1_xyz1, l1_points1 = self.sa1(l0_xyz1, l0_points1)  # (b, 1024, 3), (b, 1024, 64)
        l2_xyz1, l2_points1 = self.sa2(l1_xyz1, l1_points1)  # (b, 256, 3), (b, 256, 128)
        l3_xyz1, l3_points1 = self.sa3(l2_xyz1, l2_points1)  # (b, 64, 3), (b, 64, 256)
        l4_xyz1, l4_points1 = self.sa4(l3_xyz1, l3_points1)  # (b, 16, 3), (b, 16, 512)

        # npm3d encoder (share weighted)
        l1_xyz2, l1_points2 = self.sa1(l0_xyz2, l0_points2)  # (b, 1024, 3), (b, 1024, 64)
        l2_xyz2, l2_points2 = self.sa2(l1_xyz2, l1_points2)  # (b, 256, 3), (b, 256, 128)
        l3_xyz2, l3_points2 = self.sa3(l2_xyz2, l2_points2)  # (b, 64, 3), (b, 64, 256)
        l4_xyz2, l4_points2 = self.sa4(l3_xyz2, l3_points2)  # (b, 16, 3), (b, 16, 512)

        # semantic3d decoder
        l3_points1 = self.fp4(l3_xyz1, l4_xyz1, l3_points1, l4_points1)  # (b, 64, 256)
        l2_points1 = self.fp3(l2_xyz1, l3_xyz1, l2_points1, l3_points1)  # (b, 256, 256)
        l1_points1 = self.fp2(l1_xyz1, l2_xyz1, l1_points1, l2_points1)  # (b, 1024, 128)
        l0_points1 = self.fp1(l0_xyz1, l1_xyz1, None, l1_points1)  # (b, N, 128)
        l0_points1 = l0_points1.permute(0, 2, 1)  # (b, 128, N)
        semantic3d_points = self.drop1(F.relu(self.bn1(self.conv1(l0_points1))))  # (b, 128, N)
        semantic3d_points = self.conv2(semantic3d_points)  # (b, num_classes, N)
        semantic3d_prob = F.log_softmax(semantic3d_points, dim=1)  # (b, num_classes, N)
        semantic3d_prob = semantic3d_prob.permute(0, 2, 1)  # (b, N, num_classes)

        # npm3d decoder (folding)
        global_feature, _ = torch.max(l4_points2, dim=1)  # (b, 512)
        npm_pc_reconstructed = self.fold(global_feature)  # (b, 3, 65^2)
        npm_pc_reconstructed = npm_pc_reconstructed.permute(0, 2, 1)  # (b, 65^2, 3)

        return semantic3d_prob, npm_pc_reconstructed


if __name__ == '__main__':
    model = \
        (8)