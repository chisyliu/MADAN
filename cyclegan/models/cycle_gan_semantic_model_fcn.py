import itertools
import sys

import torch
import torch.nn.functional as F
from util.image_pool import ImagePool

from . import networks
from .base_model import BaseModel

sys.path.append('/nfs/project/libo_iMADAN')
from cycada.models import get_model


class CycleGANSemanticModel(BaseModel):
	def name(self):
		return 'CycleGANModel'
	
	def initialize(self, opt):
		BaseModel.initialize(self, opt)
		
		# specify the training losses you want to print out. The program will call base_model.get_current_losses
		self.loss_names = ['D_A', 'G_A', 'cycle_A', 'idt_A',
		                   'D_B', 'G_B', 'cycle_B', 'idt_B',
		                   'sem_AB']
		
		# specify the images you want to save/display. The program will call base_model.get_current_visuals
		visual_names_A = ['real_A', 'fake_B', 'rec_A']
		visual_names_B = ['real_B', 'fake_A', 'rec_B']
		if self.isTrain and self.opt.lambda_identity > 0.0:
			visual_names_A.append('idt_A')
			visual_names_B.append('idt_B')
		
		self.visual_names = visual_names_A + visual_names_B
		# specify the models you want to save to the disk. The program will call base_model.save_networks and base_model.load_networks
		if self.isTrain:
			self.model_names = ['G_A', 'G_B', 'D_A', 'D_B']
		
		else:  # during test time, only load Gs
			self.model_names = ['G_A', 'G_B']
		
		# load/define networks
		# The naming conversion is different from those used in the paper
		# Code (paper): G_A (G), G_B (F), D_A (D_Y), D_B (D_X)
		self.netG_A = networks.define_G(opt.input_nc, opt.output_nc,
		                                opt.ngf, opt.which_model_netG, opt.norm,
		                                not opt.no_dropout, opt.init_type, self.gpu_ids)
		self.netG_B = networks.define_G(opt.output_nc, opt.input_nc,
		                                opt.ngf, opt.which_model_netG, opt.norm,
		                                not opt.no_dropout, opt.init_type, self.gpu_ids)
		
		if self.isTrain:
			use_sigmoid = opt.no_lsgan
			self.netD_A = networks.define_D(opt.output_nc, opt.ndf,
			                                opt.which_model_netD,
			                                opt.n_layers_D, opt.norm, use_sigmoid,
			                                opt.init_type, self.gpu_ids)
			self.netD_B = networks.define_D(opt.input_nc, opt.ndf,
			                                opt.which_model_netD,
			                                opt.n_layers_D, opt.norm, use_sigmoid,
			                                opt.init_type, self.gpu_ids)
			
			# Here for semantic consistency loss, load a fcn network as fs here.
			self.netPixelCLS = get_model(opt.fcn_model, num_cls=opt.num_cls, pretrained=True, weights_init=opt.weights_init)
			# Specially initialize Pixel CLS network
			if len(self.gpu_ids) > 0:
				assert (torch.cuda.is_available())
				self.netPixelCLS.to(self.gpu_ids[0])
				self.netPixelCLS = torch.nn.DataParallel(self.netPixelCLS, self.gpu_ids)
		
		if self.isTrain:
			self.fake_A_pool = ImagePool(opt.pool_size)
			self.fake_B_pool = ImagePool(opt.pool_size)
			# define loss functions
			self.criterionGAN = networks.GANLoss(use_lsgan=not opt.no_lsgan).to(self.device)
			self.criterionCycle = torch.nn.L1Loss()
			self.criterionIdt = torch.nn.L1Loss()
			# self.criterionCLS = torch.nn.modules.CrossEntropyLoss()
			self.criterionSemantic = torch.nn.KLDivLoss(reduction='mean')
			# initialize optimizers
			self.optimizer_G = torch.optim.Adam(itertools.chain(self.netG_A.parameters(), self.netG_B.parameters()),
			                                    lr=opt.lr, betas=(opt.beta1, 0.999))
			self.optimizer_D = torch.optim.Adam(itertools.chain(self.netD_A.parameters(), self.netD_B.parameters()),
			                                    lr=opt.lr, betas=(opt.beta1, 0.999))
			
			self.optimizers = []
			self.optimizers.append(self.optimizer_G)
			self.optimizers.append(self.optimizer_D)
	
	def set_input(self, input):
		AtoB = self.opt.which_direction == 'AtoB'
		self.real_A = input['A' if AtoB else 'B'].to(self.device)
		self.real_B = input['B' if AtoB else 'A'].to(self.device)
		self.image_paths = input['A_paths' if AtoB else 'B_paths']
		if 'A_label' in input and 'B_label' in input:
			self.input_A_label = input['A_label' if AtoB else 'B_label'].to(self.device)
			self.input_B_label = input['B_label' if AtoB else 'A_label'].to(self.device)
	
	# self.image_paths = input['B_paths'] # Hack!! forcing the labels to corresopnd to B domain
	
	def forward(self):
		self.fake_B = self.netG_A(self.real_A)
		self.rec_A = self.netG_B(self.fake_B)
		
		self.fake_A = self.netG_B(self.real_B)
		self.rec_B = self.netG_A(self.fake_A)
		
		if self.isTrain:
			# Forward all four images through classifier
			# Keep predictions from fake images only
			self.pred_real_A = self.netPixelCLS(self.real_A)
			_, self.gt_pred_A = self.pred_real_A.max(1)
			
			self.pred_fake_B = self.netPixelCLS(self.fake_B)
			_, pfB = self.pred_fake_B.max(1)
	
	def backward_D_basic(self, netD, real, fake):
		# Real
		pred_real = netD(real)
		loss_D_real = self.criterionGAN(pred_real, True)
		# Fake
		pred_fake = netD(fake.detach())
		loss_D_fake = self.criterionGAN(pred_fake, False)
		# Combined Loss
		loss_D = (loss_D_real + loss_D_fake) * 0.5
		# backward
		loss_D.backward()
		return loss_D
	
	def backward_PixelCLS(self):
		label_A = self.input_A_label
		# forward only real source image through semantic classifier
		pred_A = self.netPixelCLS(self.real_A)
		self.loss_PixelCLS = self.criterionSemantic(F.log_softmax(pred_A, dim=1), label_A.long())
		self.loss_PixelCLS.backward()
	
	def backward_D_A(self):
		fake_B = self.fake_B_pool.query(self.fake_B)
		self.loss_D_A = self.backward_D_basic(self.netD_A, self.real_B, fake_B)
	
	def backward_D_B(self):
		fake_A = self.fake_A_pool.query(self.fake_A)
		self.loss_D_B = self.backward_D_basic(self.netD_B, self.real_A, fake_A)
	
	def backward_G(self, opt):
		lambda_idt = self.opt.lambda_identity
		lambda_A = self.opt.lambda_A
		lambda_B = self.opt.lambda_B
		# Identity loss
		if lambda_idt > 0:
			# G_A should be identity if real_B is fed.
			self.idt_A = self.netG_A(self.real_B)
			self.loss_idt_A = self.criterionIdt(self.idt_A, self.real_B) * lambda_B * lambda_idt
			# G_B should be identity if real_A is fed.
			self.idt_B = self.netG_B(self.real_A)
			self.loss_idt_B = self.criterionIdt(self.idt_B, self.real_A) * lambda_A * lambda_idt
		else:
			self.loss_idt_A = 0
			self.loss_idt_B = 0
		
		# GAN loss D_A(G_A(A))
		self.loss_G_A = 2 * self.criterionGAN(self.netD_A(self.fake_B), True)
		# GAN loss D_B(G_B(B))
		self.loss_G_B = self.criterionGAN(self.netD_B(self.fake_A), True)
		# Forward cycle loss
		self.loss_cycle_A = self.criterionCycle(self.rec_A, self.real_A) * lambda_A
		# Backward cycle loss
		self.loss_cycle_B = self.criterionCycle(self.rec_B, self.real_B) * lambda_B
		# combined loss standard cyclegan
		self.loss_G = self.loss_G_A + self.loss_G_B + self.loss_cycle_A + self.loss_cycle_B + self.loss_idt_A + self.loss_idt_B
		
		# real_A(syn)->fake_B->(fcn_frozen)->pred_fake_B == input_A_label
		if opt.semantic_loss:
			if opt.with_label:
				self.loss_sem_AB = self.criterionSemantic(F.log_softmax(self.pred_fake_B, dim=1), self.input_A_label)
			else:
				self.loss_sem_AB = opt.dynamic_weight * self.criterionSemantic(F.log_softmax(self.pred_fake_B, dim=1), F.softmax(self.pred_real_A,
					                                                                                                             dim=1))
			self.loss_sem_AB = opt.general_semantic_weight * self.loss_sem_AB
			self.loss_G += self.loss_sem_AB
		
		self.loss_G.backward()
	
	def optimize_parameters(self, opt):
		# forward
		self.forward()
		# G_A and G_B
		self.set_requires_grad([self.netD_A, self.netD_B], False)
		self.optimizer_G.zero_grad()
		# self.optimizer_CLS.zero_grad()
		self.backward_G(opt)
		self.optimizer_G.step()
		# D_A and D_B
		self.set_requires_grad([self.netD_A, self.netD_B], True)
		self.optimizer_D.zero_grad()
		self.backward_D_A()
		self.backward_D_B()
		self.optimizer_D.step()
