#%% imports
from numpy.lib import emath
import torch
from torch.functional import norm
import torch.nn as nn
import torch.nn.functional as F
from torch.serialization import load
import torchvision
import numpy as np
import lpips
import visdom
from utils import *
from torch.nn.utils import parameters_to_vector as ptv

#%% hparams
dataset = 'celeba'
if dataset == 'mnist':
    batch_size = 128
    img_size = 32
    n_channels = 1
    nf = 64
    ch_mult = [1,2]
    attn_resolutions = [8]
    res_blocks = 1
    disc_layers = 1
    codebook_size = 10
    emb_dim = 64
    disc_start_step = 2000
elif dataset == 'cifar10':
    batch_size = 128
    img_size = 32
    n_channels = 3
    nf = 64
    ch_mult = [1,2]
    attn_resolutions = [8]
    res_blocks = 1
    disc_layers = 1
    codebook_size = 128
    emb_dim = 256
    disc_start_step = 10000
elif dataset == 'flowers':
    batch_size = 128
    img_size = 32
    n_channels = 3
    nf = 64
    ae_blocks = 2
    codebook_size = 128
    emb_dim = 128
elif dataset == 'celeba':
    batch_size = 3
    img_size = 256
    n_channels = 3
    nf = 128
    ch_mult = [1, 1, 2, 2, 4]
    attn_resolutions = [16]
    res_blocks = 2
    disc_layers = 3
    codebook_size = 256
    emb_dim = 1024
    disc_start_step = 30001

base_lr = 4.5e-6
lr = base_lr * batch_size
train_steps = 1000001
steps_per_log = 10
steps_per_eval = 100
steps_per_checkpoint = 1000

LOAD_MODEL = True
LOAD_MODEL_STEP = 100000

#%% helper functions
def normalize(in_channels):
    return torch.nn.GroupNorm(num_groups=32, num_channels=in_channels, eps=1e-6, affine=True)


def swish(x):
    return x*torch.sigmoid(x)


def adopt_weight(weight, global_step, threshold=0, value=0.):
    if global_step < threshold:
        weight = value
    return weight


def hinge_d_loss(logits_real, logits_fake):
    loss_real = torch.mean(F.relu(1. - logits_real))
    loss_fake = torch.mean(F.relu(1. + logits_fake))
    d_loss = 0.5 * (loss_real + loss_fake)
    return d_loss


def calculate_adaptive_weight(recon_loss, g_loss, last_layer, disc_weight=0.8):
        recon_grads = torch.autograd.grad(recon_loss, last_layer, retain_graph=True)[0]
        g_grads = torch.autograd.grad(g_loss, last_layer, retain_graph=True)[0]

        d_weight = torch.norm(recon_grads) / (torch.norm(g_grads) + 1e-4)
        d_weight = torch.clamp(d_weight, 0.0, 1e4).detach()
        return d_weight * disc_weight 


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        nn.init.normal_(m.weight.data, 0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        nn.init.normal_(m.weight.data, 1.0, 0.02)
        nn.init.constant_(m.bias.data, 0)


#%% Define VQVAE classes
# From taming transformers
class VectorQuantizer(nn.Module):
    def __init__(self, n_e, e_dim, beta):
        super(VectorQuantizer, self).__init__()
        self.n_e = n_e # number of embeddings
        self.e_dim = e_dim # dimension of embedding
        self.beta = beta # commitment cost used in loss term, beta * ||z_e(x)-sg[e]||^2

        self.embedding = nn.Embedding(self.n_e, self.e_dim)
        self.embedding.weight.data.uniform_(-1.0 / self.n_e, 1.0 / self.n_e)

    def forward(self, z):
        # reshape z -> (batch, height, width, channel) and flatten
        z = z.permute(0, 2, 3, 1).contiguous()
        z_flattened = z.view(-1, self.e_dim)

        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z
        d = (z_flattened ** 2).sum(dim=1, keepdim=True) + (self.embedding.weight**2).sum(1) - \
            2 * torch.matmul(z_flattened, self.embedding.weight.t())

        # find closest encodings
        min_encoding_indices = torch.argmin(d, dim=1).unsqueeze(1)
        min_encodings = torch.zeros(min_encoding_indices.shape[0], self.n_e).to(z)
        min_encodings.scatter_(1, min_encoding_indices, 1)

        # get quantized latent vectors
        z_q = torch.matmul(min_encodings, self.embedding.weight).view(z.shape)
        # compute loss for embedding
        loss = torch.mean((z_q.detach()-z)**2) + self.beta * torch.mean((z_q - z.detach()) ** 2)
        # preserve gradients
        z_q = z + (z_q - z).detach()

        # perplexity
        e_mean = torch.mean(min_encodings, dim=0)
        perplexity = torch.exp(-torch.sum(e_mean * torch.log(e_mean + 1e-10)))
        # reshape back to match original input shape
        z_q = z_q.permute(0, 3, 1, 2).contiguous()

        return z_q, loss, (perplexity, min_encodings, min_encoding_indices)

    def get_codebook_entry(self, indices, shape):
        min_encodings = torch.zeros(indices.shape[0], self.n_e).to(indices)
        min_encodings.scatter_(1, indices[:,None], 1)
        # get quantized latent vectors
        z_q = torch.matmul(min_encodings.float(), self.embedding.weight)

        if shape is not None: # reshape back to match original input shape
            z_q = z_q.view(shape).permute(0, 3, 1, 2).contiguous()

        return z_q


class Downsample(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.conv = torch.nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=2, padding=0)
                                
    def forward(self, x):
        pad = (0,1,0,1)
        x = torch.nn.functional.pad(x, pad, mode="constant", value=0)
        x = self.conv(x)
        return x


class Upsample(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.conv = nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=1, padding=1)

    def forward(self, x):
        x = F.interpolate(x, scale_factor=2.0, mode='nearest')
        x = self.conv(x)

        return x


class ResBlock(nn.Module):
    def __init__(self, in_channels, out_channels=None):
        super(ResBlock, self).__init__()
        self.in_channels = in_channels
        self.out_channels = in_channels if out_channels is None else out_channels
        self.norm1 = normalize(in_channels)
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding=1)
        self.norm2 = normalize(out_channels)
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1)
        self.conv_out = nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=1, padding=0)

    def forward(self, x_in):
        x = x_in
        x = self.norm1(x)
        x = swish(x)
        x = self.conv1(x)
        x = self.norm2(x)
        x = swish(x)
        x = self.conv2(x)
        if self.in_channels != self.out_channels:
            x_in = self.conv_out(x_in)

        return x + x_in


class AttnBlock(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.in_channels = in_channels

        self.norm = normalize(in_channels)
        self.q = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.k = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.v = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.proj_out = torch.nn.Conv2d(in_channels,
                                        in_channels,
                                        kernel_size=1,
                                        stride=1,
                                        padding=0)


    def forward(self, x):
        h_ = x
        h_ = self.norm(h_)
        q = self.q(h_)
        k = self.k(h_)
        v = self.v(h_)

        # compute attention
        b,c,h,w = q.shape
        q = q.reshape(b,c,h*w)
        q = q.permute(0,2,1)   # b,hw,c
        k = k.reshape(b,c,h*w) # b,c,hw
        w_ = torch.bmm(q,k)     # b,hw,hw    w[b,i,j]=sum_c q[b,i,c]k[b,c,j]
        w_ = w_ * (int(c)**(-0.5))
        w_ = F.softmax(w_, dim=2)

        # attend to values
        v = v.reshape(b,c,h*w)
        w_ = w_.permute(0,2,1)   # b,hw,hw (first hw of k, second of q)
        h_ = torch.bmm(v,w_)     # b, c,hw (hw of q) h_[b,c,j] = sum_i v[b,c,i] w_[b,i,j]
        h_ = h_.reshape(b,c,h,w)

        h_ = self.proj_out(h_)

        return x+h_


class Encoder(nn.Module):
    def __init__(self, in_channels, nf, out_channels, ch_mults, num_res_blocks, resolution, attn_resolutions):
        super().__init__()
        self.nf = nf
        self.num_resolutions = len(ch_mults)
        self.num_res_blocks = num_res_blocks
        self.resolution = resolution
        self.attn_resolutions = attn_resolutions 

        
        curr_res = self.resolution
        in_ch_mults = (1,)+tuple(ch_mults)
        
        blocks = []
        # initial convultion
        blocks.append(nn.Conv2d(in_channels, nf, kernel_size=3, stride=1, padding=1))
        
        # residual and downsampling blocks, with attention on smaller res (16x16)
        for i in range(self.num_resolutions):
            block_in_ch = nf * in_ch_mults[i]
            block_out_ch = nf * ch_mults[i]
            for _ in range(self.num_res_blocks):
                blocks.append(ResBlock(block_in_ch, block_out_ch))
                block_in_ch = block_out_ch
                if curr_res in attn_resolutions:
                    blocks.append(AttnBlock(block_in_ch))
            
            if i != self.num_resolutions -1:
                blocks.append(Downsample(block_in_ch))
                curr_res = curr_res // 2
        
        # non-local attention block
        blocks.append(ResBlock(block_in_ch, block_in_ch))
        blocks.append(AttnBlock(block_in_ch))
        blocks.append(ResBlock(block_in_ch, block_in_ch))

        # normalise and convert to latent size
        blocks.append(normalize(block_in_ch))
        blocks.append(nn.Conv2d(block_in_ch, out_channels, kernel_size=3, stride=1, padding=1))
        self.blocks = nn.ModuleList(blocks)

    def forward(self, x):
        for block in self.blocks:
            # print(block)
            x = block(x)
        return x


class Generator(nn.Module):
    def __init__(self, in_channels, nf, out_channels, ch_mults, num_res_blocks, resolution, attn_resolutions):
        super().__init__()
        self.nf = nf
        self.num_resolutions = len(ch_mults)
        self.num_res_blocks = num_res_blocks
        self.resolution = resolution
        self.attn_resolutions = attn_resolutions 

        block_in_ch = nf * ch_mults[-1]
        curr_res = self.resolution // 2 ** (self.num_resolutions-1)

        blocks = []
        # initial conv
        blocks.append(nn.Conv2d(in_channels, block_in_ch, kernel_size=3, stride=1, padding=1))
        
        # non-local attention block
        blocks.append(ResBlock(block_in_ch, block_in_ch))
        blocks.append(AttnBlock(block_in_ch))    
        blocks.append(ResBlock(block_in_ch, block_in_ch))

        for i in reversed(range(self.num_resolutions)):
            block_out_ch = nf * ch_mults[i]

            for _ in range(self.num_res_blocks):
                blocks.append(ResBlock(block_in_ch, block_out_ch))
                block_in_ch = block_out_ch

                if curr_res in self.attn_resolutions:
                    blocks.append(AttnBlock(block_in_ch))

            if i != 0:
                blocks.append(Upsample(block_in_ch))
                curr_res = curr_res * 2

        blocks.append(normalize(block_in_ch))
        blocks.append(nn.Conv2d(block_in_ch, out_channels, kernel_size=3, stride=1, padding=1))

        self.blocks = nn.ModuleList(blocks)

    def forward(self, x):
        for block in self.blocks:
            x = block(x)
        return x

class VQAutoEncoder(nn.Module):
    def __init__(self, in_channels, nf, n_blocks, n_e, embed_dim, ch_mults, resolution, attn_resolutions, beta=0.25):
        super().__init__()
        self.encoder = Encoder(in_channels, nf, embed_dim, ch_mults, n_blocks, resolution, attn_resolutions)
        self.quantize = VectorQuantizer(n_e, embed_dim, beta)
        self.generator = Generator(embed_dim, nf, in_channels, ch_mults, n_blocks, resolution, attn_resolutions)

    def forward(self, x):
        x = self.encoder(x)
        quant, codebook_loss, _ = self.quantize(x)
        x = self.generator(quant)
        return x, codebook_loss

# patch based discriminator
class Discriminator(nn.Module):
    def __init__(self, nc, nf, n_layers=3, factor=1.0, weight=0.8):
        super().__init__()
        self.disc_factor = factor
        self.disc_weight = weight
        layers = [nn.Conv2d(nc, nf, kernel_size=4, stride=2, padding=1), nn.LeakyReLU(0.2, True)]
        nf_mult = 1
        nf_mult_prev = 1
        for n in range(1, n_layers):  # gradually increase the number of filters
            nf_mult_prev = nf_mult
            nf_mult = min(2 ** n, 8)
            layers += [
                nn.Conv2d(nf * nf_mult_prev, nf * nf_mult, kernel_size=4, stride=2, padding=1, bias=False),
                nn.BatchNorm2d(nf * nf_mult),
                nn.LeakyReLU(0.2, True)
            ]
        
        nf_mult_prev = nf_mult
        nf_mult = min(2 ** n_layers, 8)

        layers += [
            nn.Conv2d(nf * nf_mult_prev, nf * nf_mult, kernel_size=4, stride=1, padding=1, bias=False),
            nn.BatchNorm2d(nf * nf_mult),
            nn.LeakyReLU(0.2, True)
        ]

        layers += [
            nn.Conv2d(nf * nf_mult, 1, kernel_size=4, stride=1, padding=1)]  # output 1 channel prediction map
        self.main = nn.Sequential(*layers)   
    
    def forward(self, x):
        return self.main(x)

# %% main training loop
def main(): 
    train_iterator = cycle(get_data_loader(dataset, img_size, batch_size))
    
    autoencoder = VQAutoEncoder(n_channels, nf, res_blocks, codebook_size, emb_dim, ch_mult, img_size, attn_resolutions).cuda()
    discriminator = Discriminator(n_channels, nf, n_layers=disc_layers).cuda()
    perceptual_loss = lpips.LPIPS(net='vgg').cuda()

    ae_optim = torch.optim.Adam(autoencoder.parameters(), lr=lr)
    d_optim = torch.optim.Adam(discriminator.parameters(), lr=lr)

    start_step = 0 
    if LOAD_MODEL:
        autoencoder = load_model(autoencoder, 'ae', LOAD_MODEL_STEP, log_dir)
        discriminator = load_model(discriminator, 'discriminator', LOAD_MODEL_STEP, log_dir)
        ae_optim = load_model(ae_optim, 'ae_optim', LOAD_MODEL_STEP, log_dir)
        d_optim = load_model(d_optim, 'disc_optim', LOAD_MODEL_STEP, log_dir)
        start_step = LOAD_MODEL_STEP

    log(f'AE Parameters: {len(ptv(autoencoder.parameters()))}')
    log(f'Discriminator Parameters: {len(ptv(discriminator.parameters()))}')

    g_losses, d_losses = np.array([]), np.array([])

    for step in range(start_step, train_steps):
        x, _ = next(train_iterator)
        x = x.cuda()
        x_hat, codebook_loss = autoencoder(x)
        
        # get recon/perceptual loss
        recon_loss = torch.abs(x.contiguous() - x_hat.contiguous()) # L1 loss
        p_loss = perceptual_loss(x.contiguous(), x_hat.contiguous())
        nll_loss = recon_loss + p_loss
        nll_loss = torch.mean(nll_loss)

        # update generator on every training step
        logits_fake = discriminator(x_hat.contiguous())
        g_loss = -torch.mean(logits_fake)
        last_layer = autoencoder.generator.blocks[-1].weight
        d_weight = calculate_adaptive_weight(nll_loss, g_loss, last_layer)
        d_weight *= adopt_weight(1, step, disc_start_step)
        loss = nll_loss + d_weight * g_loss + codebook_loss
        g_losses = np.append(g_losses, loss.item())

        ae_optim.zero_grad()
        loss.backward()
        ae_optim.step()

        # update discriminator
        if step > disc_start_step:
            logits_real = discriminator(x.contiguous().detach()) # detach so that generator isn't also updated
            logits_fake = discriminator(x_hat.contiguous().detach())
            d_loss = hinge_d_loss(logits_real, logits_fake)
            d_losses = np.append(d_losses, d_loss.item())

            d_optim.zero_grad()
            d_loss.backward()
            d_optim.step()

        if step % steps_per_log == 0:
            if len(d_losses) == 0:
                d_loss_str = 'N/A'
            else:
                d_loss_str = f'{d_losses.mean():.3f}'
            
            log(f"Step {step}  G Loss: {g_losses.mean():.3f}  D Loss: {d_loss_str}  L1: {recon_loss.mean().item():.3f}  Perceptual: {p_loss.mean().item():.3f}  Disc: {g_loss.item():.3f}")
            g_losses, d_losses = np.array([]), np.array([])
            vis.images(x.clamp(0,1)[:64], win="x", nrow=int(np.sqrt(batch_size)), opts=dict(title="x"))
            vis.images(x_hat.clamp(0,1)[:64], win="recons", nrow=int(np.sqrt(batch_size)), opts=dict(title="recons"))
            
        if step % steps_per_eval == 0:
            save_images(x_hat[:64], vis, 'recons', step, log_dir)

        if step % steps_per_checkpoint == 0 and step > 0 and not (LOAD_MODEL and step == LOAD_MODEL_STEP):
            print("Saving model")
            save_model(autoencoder, 'ae', step, log_dir)
            save_model(discriminator, 'discriminator', step, log_dir)
            save_model(ae_optim, 'ae_optim', step, log_dir)
            save_model(d_optim, 'disc_optim', step, log_dir)

#%% main
if __name__ == '__main__':
    vis = visdom.Visdom()
    log_dir = f'new_vq_gan_test_{dataset}'
    config_log(log_dir)
    start_training_log(dict(
        dataset = dataset,
        batch_size = batch_size,
        img_size = img_size,
        n_channels = n_channels,
        nf=nf,
        ch_mult=ch_mult,
        attn_resolutions=attn_resolutions,
        res_blocks=res_blocks,
        disc_layers=disc_layers,
        disc_start_step=disc_start_step,
        codebook_size = codebook_size,
        emb_dim = emb_dim,
    ))
    main()