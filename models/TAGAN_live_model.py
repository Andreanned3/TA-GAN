import torch
from .base_model import BaseModel
from . import networks
from torchvision import models, transforms
import itertools
import numpy
import tifffile

class TAGANLiveModel(BaseModel):
    """ This class implements the TA-GAN model for confocal to STED resolution enhancement for the F-Actin Live dataset.

    Original code taken from pix2pix paper: https://arxiv.org/pdf/1611.07004.pdf
    """
    @staticmethod
    def modify_commandline_options(parser, is_train=True):
        """Add new dataset-specific options, and rewrite default values for existing options.

        Parameters:
            parser          -- original option parser
            is_train (bool) -- whether training phase or test phase. You can use this flag to add training-specific or test-specific options.

        Returns:
            the modified parser.
        """
        # changing the default values to match the pix2pix paper (https://phillipi.github.io/pix2pix/)
        parser.set_defaults(norm='batch', netG='resnet_9blocks', netS='unet_128', dataset_mode='live')
        if is_train:
            parser.set_defaults(pool_size=0, gan_mode='vanilla', batch_size=16, niter=5000, niter_decay=0, preprocess='crop_rotation', crop_size=256, save_epoch_freq=100)
            parser.add_argument('--lambda_seg', type=float, default=1.0, help='weight for seg loss')
            parser.add_argument('--lambda_GAN', type=float, default=10.0, help='weight for GAN loss')
        return parser

    def __init__(self, opt):
        """Initialize the pix2pix class.

        Parameters:
            opt (Option class)-- stores all the experiment flags; needs to be a subclass of BaseOptions
        """
        BaseModel.__init__(self, opt)
        # specify the training losses you want to print out. The training/test scripts will call <BaseModel.get_current_losses>
        self.loss_names = ['G_GAN', 'D_real', 'D_fake', 'S_fake']
        # specify the images you want to save/display. The training/test scripts will call <BaseModel.get_current_visuals>
        #self.visual_names = ['input', 'real_B', 'seg_rB', 'seg_fB', 'fake_B', 'S', 'seg_GT', 'decision_map']
        self.visual_names = ['input', 'real_B', 'seg_fB', 'fake_B', 'seg_rB']
        if not self.isTrain:
            self.isValid = False

        # specify the models you want to save to the disk. The training/test scripts will call <BaseModel.save_networks> and <BaseModel.load_networks>
        if self.isTrain:
            self.model_names = ['G', 'D', 'S']
        else:  # during test time, only load G
            self.model_names = ['G', 'S']
        # define networks (generator, discriminator and reference VGG)
        self.netG = networks.define_G(3, opt.output_nc, opt.ngf, opt.netG, opt.norm,
                                      not opt.no_dropout, opt.init_type, opt.init_gain, self.gpu_ids) # not opt.no_dropout

        #self.netG2 = networks.define_CNN(gpu_ids=self.gpu_ids)

        self.netS = networks.define_S(opt.output_nc, 2, opt.ngf, opt.netS, opt.norm, False, opt.init_type, opt.init_gain, self.gpu_ids)

        if self.isTrain:  # define a discriminator; conditional GANs need to take both input and output images; Therefore, #channels for D is input_nc + output_nc
            self.netD = networks.define_D(opt.input_nc+opt.output_nc, opt.ndf, opt.netD,
                                          opt.n_layers_D, opt.norm, opt.init_type, opt.init_gain, self.gpu_ids)

        if self.isTrain:
            # define loss functions
            self.criterionGAN = networks.GANLoss(opt.gan_mode).to(self.device)
            self.criterionSEG = torch.nn.MSELoss()
            # initialize optimizers; schedulers will be automatically created by function <BaseModel.setup>.
            #self.optimizer_G = torch.optim.Adam(itertools.chain(self.netG.parameters(), self.netG2.parameters()), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_G = torch.optim.Adam(self.netG.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.optimizer_D = torch.optim.Adam(self.netD.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))

            self.optimizers.append(self.optimizer_G)
            self.optimizers.append(self.optimizer_D)

    def set_input(self, input):
        """Unpack input data from the dataloader and perform necessary pre-processing steps.

        Parameters:
            input (dict): include the data itself and its metadata information.

        The option 'direction' can be used to swap images in domain A and domain B.
        """
        AtoB = self.opt.direction == 'AtoB'
        self.real_A = input['A' if AtoB else 'B'].to(self.device) # -1-1 
        self.real_B = input['B' if AtoB else 'A'].to(self.device) # -1-1 
        self.decision_map = input['decision_map'].to(self.device) # 0-1
        # Concatenate confocal and STED_map to two channels input
        self.S = self.real_B * self.decision_map
        self.S[self.decision_map==0] = -1
        self.decision_map[self.decision_map==0] = -1
        self.input = torch.cat((self.real_A, self.S, self.decision_map), 1)
        #self.input = torch.cat((self.real_A, self.S), 1)
        self.image_paths = input['A_paths' if AtoB else 'B_paths']

    def forward(self):
        """Run forward pass; called by both functions <optimize_parameters> and <test>."""
        self.fake_B = self.netG(self.input)
        self.seg_fB = self.netS(self.fake_B)
        self.seg_rB = self.netS(self.real_B).detach()
        # Threshold the segmentation map
        self.seg_rB[:,0,:,:] = self.seg_rB[:,0,:,:]>(0.06667*2-1)
        self.seg_rB[:,1,:,:] = self.seg_rB[:,1,:,:]>(0.03922*2-1)


    def backward_D(self):
        """Calculate GAN loss for the discriminator"""
        fake_AB = torch.cat((self.real_A, self.fake_B), 1)  # we use conditional GANs; we need to feed both input and output to the discriminator
        pred_fake = self.netD(fake_AB.detach())
        self.loss_D_fake = self.criterionGAN(pred_fake, False)
        real_AB = torch.cat((self.real_A, self.real_B), 1)
        pred_real = self.netD(real_AB)
        self.loss_D_real = self.criterionGAN(pred_real, True)
        # Fake; stop backprop to the generator by detaching fake_B
        self.loss_D = (self.loss_D_fake + self.loss_D_real) * 0.5
        self.loss_D.backward()

    def backward_G(self):
        """Calculate GAN and MSE loss for the generator"""
        # First, G(A) should fake the discriminator
        fake_AB = torch.cat((self.real_A, self.fake_B), 1)
        pred_fake = self.netD(fake_AB)
        self.loss_G_GAN = self.criterionGAN(pred_fake, True) * self.opt.lambda_GAN

        # Segmentation loss S(G(A)) = S(B)
        self.loss_S_fake = self.criterionSEG(self.seg_fB, self.seg_rB) * self.opt.lambda_seg

        # combine loss and calculate gradients
        self.loss_G = self.loss_G_GAN + self.loss_S_fake
        self.loss_G.backward()

    def optimize_parameters(self):
        self.forward()                   # compute fake images: G(A)
        # update D
        self.set_requires_grad(self.netD, True)  # enable backprop for D
        self.optimizer_D.zero_grad()     # set D's gradients to zero
        self.backward_D()                # calculate gradients for D
        self.optimizer_D.step()          # update D's weights
        # update G
        self.set_requires_grad(self.netD, False)  # D requires no gradients when optimizing G and S
        self.optimizer_G.zero_grad()        # set G's gradients to zero
        self.backward_G()                   # calculate gradients for G
        self.optimizer_G.step()             # udpate G's weights
