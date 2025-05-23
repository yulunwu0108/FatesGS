import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import List, Union
from collections import OrderedDict


def bin_op_reduce(lst, func):
    result = lst[0]
    for i in range(1, len(lst)):
        result = func(result, lst[i])
    return result


def idx_world2cam(idx_world_homo, cam):
    """nhw41 -> nhw41"""
    idx_cam_homo = cam[:,0:1,...].unsqueeze(1) @ idx_world_homo  # nhw41
    idx_cam_homo = idx_cam_homo / (idx_cam_homo[...,-1:,:]+1e-9)   # nhw41
    return idx_cam_homo


def idx_cam2img(idx_cam_homo, cam):
    """nhw41 -> nhw31"""
    idx_cam = idx_cam_homo[...,:3,:] / (idx_cam_homo[...,3:4,:]+1e-9)  # nhw31
    idx_img_homo = cam[:,1:2,:3,:3].unsqueeze(1) @ idx_cam  # nhw31
    idx_img_homo = idx_img_homo / (idx_img_homo[...,-1:,:]+1e-9)
    return idx_img_homo


def normalize_for_grid_sample(input_, grid):
    size = torch.tensor(input_.size())[2:].flip(0).to(grid.dtype).to(grid.device).view(1,1,1,-1)  # 111N
    grid_n = grid / size
    grid_n = (grid_n * 2 - 1).clamp(-1.1, 1.1)
    return grid_n


def get_in_range(grid):
    """after normalization, keepdim=False"""
    masks = []
    for dim in range(grid.size()[-1]):
        masks += [grid[..., dim]<=1, grid[..., dim]>=-1]
    in_range = bin_op_reduce(masks, torch.min).to(grid.dtype)
    return in_range


def load_pair(file: str, min_views: int=None):
    with open(file) as f:
        lines = f.readlines()
    n_cam = int(lines[0])
    pairs = {}
    img_ids = []
    for i in range(1, 1+2*n_cam, 2):
        pair = []
        score = []
        img_id = lines[i].strip()
        pair_str = lines[i+1].strip().split(' ')
        n_pair = int(pair_str[0])
        if min_views is not None and n_pair < min_views: continue
        for j in range(1, 1+2*n_pair, 2):
            pair.append(pair_str[j])
            score.append(float(pair_str[j+1]))
        img_ids.append(img_id)
        pairs[img_id] = {'id': img_id, 'index': i//2, 'pair': pair, 'score': score}
    pairs['id_list'] = img_ids
    return pairs


class ListModule(nn.Module):
    def __init__(self, modules: Union[List, OrderedDict]):
        super(ListModule, self).__init__()
        if isinstance(modules, OrderedDict):
            iterable = modules.items()
        elif isinstance(modules, list):
            iterable = enumerate(modules)
        else:
            raise TypeError('modules should be OrderedDict or List.')
        for name, module in iterable:
            if not isinstance(module, nn.Module):
                module = ListModule(module)
            if not isinstance(name, str):
                name = str(name)
            self.add_module(name, module)

    def __getitem__(self, idx):
        if idx < 0 or idx >= len(self._modules):
            raise IndexError('index {} is out of range'.format(idx))
        it = iter(self._modules.values())
        for i in range(idx):
            next(it)
        return next(it)

    def __iter__(self):
        return iter(self._modules.values())

    def __len__(self):
        return len(self._modules)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None, dim=2):
        super(BasicBlock, self).__init__()

        self.conv_fn = nn.Conv2d if dim == 2 else nn.Conv3d
        self.bn_fn = nn.BatchNorm2d if dim == 2 else nn.BatchNorm3d
        # self.bn_fn = nn.GroupNorm

        self.conv1 = self.conv3x3(inplanes, planes, stride)
        # nn.init.xavier_uniform_(self.conv1.weight)
        self.bn1 = self.bn_fn(planes)
        # nn.init.constant_(self.bn1.weight, 1)
        # nn.init.constant_(self.bn1.bias, 0)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = self.conv3x3(planes, planes)
        # nn.init.xavier_uniform_(self.conv2.weight)
        self.bn2 = self.bn_fn(planes)
        # nn.init.constant_(self.bn2.weight, 0)
        # nn.init.constant_(self.bn2.bias, 0)
        self.downsample = downsample
        self.stride = stride

    def conv1x1(self, in_planes, out_planes, stride=1):
        """1x1 convolution"""
        return self.conv_fn(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)

    def conv3x3(self, in_planes, out_planes, stride=1):
        """3x3 convolution with padding"""
        return self.conv_fn(in_planes, out_planes, kernel_size=3, stride=stride, padding=1, bias=False)

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


def _make_layer(inplanes, block, planes, blocks, stride=1, dim=2):
    downsample = None
    conv_fn = nn.Conv2d if dim==2 else nn.Conv3d
    bn_fn = nn.BatchNorm2d if dim==2 else nn.BatchNorm3d
    # bn_fn = nn.GroupNorm
    if stride != 1 or inplanes != planes * block.expansion:
        downsample = nn.Sequential(
            conv_fn(inplanes, planes * block.expansion, kernel_size=1, stride=stride, bias=False),
            bn_fn(planes * block.expansion)
        )

    layers = []
    layers.append(block(inplanes, planes, stride, downsample, dim=dim))
    inplanes = planes * block.expansion
    for _ in range(1, blocks):
        layers.append(block(inplanes, planes, dim=dim))

    return nn.Sequential(*layers)


class UNet(nn.Module):

    def __init__(self, inplanes: int, enc: int, dec: int, initial_scale: int,
                 bottom_filters: List[int], filters: List[int], head_filters: List[int],
                 prefix: str, dim: int=2):
        super(UNet, self).__init__()

        conv_fn = nn.Conv2d if dim==2 else nn.Conv3d
        bn_fn = nn.BatchNorm2d if dim==2 else nn.BatchNorm3d
        # bn_fn = nn.GroupNorm
        deconv_fn = nn.ConvTranspose2d if dim==2 else nn.ConvTranspose3d
        current_scale = initial_scale
        idx = 0
        prev_f = inplanes

        self.bottom_blocks = OrderedDict()
        for f in bottom_filters:
            block = _make_layer(prev_f, BasicBlock, f, enc, 1 if idx==0 else 2, dim=dim)
            self.bottom_blocks[f'{prefix}{current_scale}_{idx}'] = block
            idx += 1
            current_scale *= 2
            prev_f = f
        self.bottom_blocks = ListModule(self.bottom_blocks)

        self.enc_blocks = OrderedDict()
        for f in filters:
            block = _make_layer(prev_f, BasicBlock, f, enc, 1 if idx == 0 else 2, dim=dim)
            self.enc_blocks[f'{prefix}{current_scale}_{idx}'] = block
            idx += 1
            current_scale *= 2
            prev_f = f
        self.enc_blocks = ListModule(self.enc_blocks)

        self.dec_blocks = OrderedDict()
        for f in filters[-2::-1]:
            block = [
                deconv_fn(prev_f, f, 3, 2, 1, 1, bias=False),
                conv_fn(2*f, f, 3, 1, 1, bias=False),
            ]
            if dec > 0:
                block.append(_make_layer(f, BasicBlock, f, dec, 1, dim=dim))
            # nn.init.xavier_uniform_(block[0].weight)
            # nn.init.xavier_uniform_(block[1].weight)
            self.dec_blocks[f'{prefix}{current_scale}_{idx}'] = block
            idx += 1
            current_scale //= 2
            prev_f = f
        self.dec_blocks = ListModule(self.dec_blocks)

        self.head_blocks = OrderedDict()
        for f in head_filters:
            block = [
                deconv_fn(prev_f, f, 3, 2, 1, 1, bias=False)
            ]
            if dec > 0:
                block.append(_make_layer(f, BasicBlock, f, dec, 1, dim=dim))
            block = nn.Sequential(*block)
            # nn.init.xavier_uniform_(block[0])
            self.head_blocks[f'{prefix}{current_scale}_{idx}'] = block
            idx += 1
            current_scale //= 2
            prev_f = f
        self.head_blocks = ListModule(self.head_blocks)

    def forward(self, x, multi_scale=1):
        for b in self.bottom_blocks:
            x = b(x)
        enc_out = []
        for b in self.enc_blocks:
            x = b(x)
            enc_out.append(x)
        dec_out = [x]
        for i, b in enumerate(self.dec_blocks):
            if len(b) == 3: deconv, post_concat, res = b
            elif len(b) == 2: deconv, post_concat = b
            x = deconv(x)
            x = torch.cat([x, enc_out[-2-i]], 1)
            x = post_concat(x)
            if len(b) == 3: x = res(x)
            dec_out.append(x)
        for b in self.head_blocks:
            x = b(x)
            dec_out.append(x)
        if multi_scale == 1: return x
        else: return dec_out[-multi_scale:]


class FeatExt(nn.Module):

    def __init__(self):
        super(FeatExt, self).__init__()
        self.init_conv = nn.Sequential(
            nn.Conv2d(3, 16, 5, 2, 2, bias=False),
            nn.BatchNorm2d(16),
            nn.ReLU()
        )
        self.unet = UNet(16, 2, 1, 2, [], [32, 64, 128], [], '2d', 2)
        self.final_conv_1 = nn.Conv2d(128, 32, 3, 1, 1, bias=False)
        self.final_conv_2 = nn.Conv2d(64, 32, 3, 1, 1, bias=False)
        self.final_conv_3 = nn.Conv2d(32, 32, 3, 1, 1, bias=False)

        feat_ext_dict = {k[16:]:v for k,v in torch.load('utils/vismvsnet.pt')['state_dict'].items() if k.startswith('module.feat_ext')}
        self.load_state_dict(feat_ext_dict)

    def forward(self, x):
        out = self.init_conv(x)
        out1, out2, out3 = self.unet(out, multi_scale=3)
        return self.final_conv_1(out1), self.final_conv_2(out2), self.final_conv_3(out3)


def get_feat_loss_corr(diff_surf_pts, feat, cam, feat_src, src_cams, mask, scale=2):
    if (mask).sum() == 0:
        return torch.tensor(0.0).float().cuda()

    # feat.size(): [B, n_channel, h, w], where h, w are down scaled: 384, 512
    sample_mask = mask.view(feat.size()[0], -1) # [B, N_rays]
    hit_nums = sample_mask.sum(-1) # [B]
    accu_nums = [0] + hit_nums.cumsum(0).tolist()
    slices = [slice(accu_nums[i], accu_nums[i + 1]) for i in range(len(accu_nums) - 1)]

    loss = []
    ## for each image in minibatch
    for view_i, slice_ in enumerate(slices):
        if slice_.start < slice_.stop:

            ## projection
            diff_surf_pts_slice = diff_surf_pts[slice_]
            pts_world = (diff_surf_pts_slice).view(1, -1, 1, 3, 1)  # / 2 * size.view(1, 1) + center.view(1, 3) 1m131, where m == n_masked_rays
            pts_world = torch.cat([pts_world, torch.ones_like(pts_world[..., -1:, :])], dim=-2)  # 1m141
            # rgb_pack = torch.cat([rgb[view_i:view_i+1], rgb_src[view_i]], dim=0)  # v3hw
            cam_pack = torch.cat([cam[view_i:view_i + 1], src_cams[view_i]], dim=0)  # v244, v == 1 + n_src; here cam is depth/feature cam upscaled by 2
            pts_img = idx_cam2img(idx_world2cam(pts_world, cam_pack), cam_pack)  # vm131
            grid = pts_img[..., :2, 0]  # vm12

            # generate visiblity mask
            c2w = torch.inverse(cam_pack[1:, 0, ...]) # [n_src (v-1), 4, 4]
            src_cam_centers = c2w[:, :3, 3] # [n_src, 3]
            dists = torch.norm(diff_surf_pts[None] - src_cam_centers[:, None], dim=-1) # [nsrc, m (num points)]

            uv = grid[1:].squeeze(2).round().long() # [nsrc, m , 2]
            vis_masks = []
            for i in range(uv.shape[0]):
                _, sorted_indices = torch.sort(dists[i]) # [m]
                sorted_uv = uv[i, sorted_indices] # [m, 2]
                _, cnts = torch.unique(sorted_uv, sorted=False, return_counts=True, dim=0)
                cnts = torch.cat((torch.tensor([0]).long().cuda(), cnts))
                unique_index = torch.cumsum(cnts, dim=0)
                unique_index = unique_index[:-1]
                sorted_vis_mask = torch.zeros_like(sorted_indices)
                sorted_vis_mask[unique_index] = 1.
                _, indices = torch.sort(sorted_indices)
                vis_mask = sorted_vis_mask[indices]
                vis_masks.append(vis_mask)

            vis_masks = torch.stack(vis_masks, dim=0) # [nsrc, m]

            ## gathering

            feat2_pack = torch.cat([feat[view_i:view_i + 1], feat_src[view_i]], dim=0) # [v, n_channel, h, w]
            grid_n = normalize_for_grid_sample(feat2_pack, grid / scale) # [v, m, 1, 2]
            grid_in_range = get_in_range(grid_n) # [v, m, 1]
            valid_mask = (grid_in_range[:1, ...] * grid_in_range[1:, ...]).unsqueeze(1) > 0.5  # [n_src, 1, m, 1]
            gathered_feat = F.grid_sample(feat2_pack, grid_n, mode='bilinear', padding_mode='zeros',
                                          align_corners=False)  # vcm1

            vis_masks = (vis_masks > 0.5).reshape_as(valid_mask)

            ## calculation
            gathered_norm = gathered_feat.norm(dim=1, keepdim=True)  # v1m1
            corr = (gathered_feat[:1] * gathered_feat[1:]).sum(dim=1, keepdim=True) \
                   / gathered_norm[:1].clamp(min=1e-9) / gathered_norm[1:].clamp(min=1e-9)  # (v-1)1m1
            corr_loss = (1 - corr).abs()

            diff_mask = corr_loss < 0.5
            sample_loss = (corr_loss * valid_mask * diff_mask * vis_masks).mean()
        else:
            sample_loss = torch.zeros(1).float().cuda()
        loss.append(sample_loss)
    loss = sum(loss) / len(loss)
    return loss


def get_feat_loss(pts_world, viewpoint_cam, src_viewpoint_stack, mask, resolution=2, use_mask=True):
    src_cams_list = []

    for src_viewpoint_cam in src_viewpoint_stack:
        src_cam = torch.zeros((2, 4, 4)).cuda()
        src_cam[0, :, :] = src_viewpoint_cam.world_view_transform.T
        src_cam[1, :, :] = src_viewpoint_cam.intrinsic
        src_cams_list.append(src_cam.unsqueeze(0))

    src_cams = torch.cat(src_cams_list, dim=0).unsqueeze(0)

    cam = torch.zeros((2, 4, 4)).cuda()
    cam[0, :, :] = viewpoint_cam.world_view_transform.T
    cam[1, :, :] = viewpoint_cam.intrinsic
    cam = cam.unsqueeze(0)

    if use_mask:
        pts_mask = mask
    else:
        pts_mask = torch.ones_like(pts_world)[:, 0]

    feat_loss_value = 0.0
    weight_sum = 0.0

    for scale in [1, 2]:
        feat_src_list = []
        for src_viewpoint_cam in src_viewpoint_stack:
            feat_src_list.append(src_viewpoint_cam.feat[scale - 1].unsqueeze(0))

        feat_src = torch.cat(feat_src_list, dim=0).unsqueeze(0)

        feat = viewpoint_cam.feat[scale - 1].unsqueeze(0)

        grid_scale = scale if resolution == 2 else 2 * scale
        feat_loss_value += (1.0 / scale) * get_feat_loss_corr(pts_world[pts_mask], feat, cam, feat_src, src_cams, pts_mask.long(), scale=grid_scale)
        weight_sum += 1.0 / scale

    return feat_loss_value / weight_sum
