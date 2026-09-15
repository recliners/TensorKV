#!/usr/bin/env python3

import datetime
import ipaddress
import hashlib
import struct
import os
from scapy.all import *
p4 = bfrt.indq_rdma.pipe
mirror = bfrt.mirror
pre = bfrt.pre

logfile = "/root/wly_experiment/indq_rdma/log_results/sketch.log"
rdma_dir = "/root/wly_experiment/indq_rdma/rdma_metadata"

store_flag = False

forwardingRules = [
("6c:ec:5a:62:a8:00", 66),    # Tofino CPU 1
("08:c0:eb:24:7b:8b", 148),   # Collector
("08:c0:eb:24:68:6b", 180)    # Generator
]

collectorEthertoPorts = [
("08:c0:eb:24:7b:8b", 148),
]

mcRules = [
	{
	"mgid":1,
	"egressPort":148,
	"duplicate_num":1
	},
	{
	"mgid":2,
	"egressPort":148,
	"duplicate_num":2
	},
	{
	"mgid":3,
	"egressPort":148,
	"duplicate_num":3
	}
]	


def log(text):
	""" 打印日志 """
	global logfile, datetime
	line = "%s \t DigProc: %s" %(str(datetime.datetime.now()), str(text))
	print(line)
	f = open(logfile, "w+")
	f.write(line + "\n")
	f.close()

def getRDMAMetadata():
	
	global log, os, rdma_dir
	
	log("Reading collector RDMA metadata from disk...")
	try:
		# 起始的数据包序列号
		f = open("%s/tmp_psn" % rdma_dir, "r")
		start_psn = int(f.read())
		f.close()

		# 队列对
		f = open("%s/tmp_qpnum" % rdma_dir, "r")
		queue_pair = int(f.read())
		f.close()
		
		# 起始内存地址
		f = open("%s/tmp_memaddr" % rdma_dir, "r")
		memory_start = int(f.read())
		f.close()
		
		# 能够用于存放数据的长度
		f = open("%s/tmp_memlen" % rdma_dir, "r")
		memory_length = int(f.read())
		f.close()
		
		# 远程键 (用于获取访问远端主机内存的权限)
		f = open("%s/tmp_rkey" % rdma_dir, "r")
		remote_key = int(f.read())
		f.close()
	except:
		log("   !!!   !!!   Failed to read RDMA metadata   !!!   !!!   ")
	
	log("Collector RDMA metadata has extracted from disk!!! ")

	return queue_pair, start_psn, memory_start, memory_length, remote_key

def storeRDMAMetadata(packet):

	global log, store_flag, struct, rdma_dir

	# 我们使用 UDP 来携带 RDMA 连接所包含的信息
	log("Receive and store RDMA connection metadata to %s" % rdma_dir)

	udp_payload = packet["UDP"].load
	psn, queue_pair, memory_start, memory_length, remote_key = struct.unpack("!IIQII", udp_payload)
	# 将解析出的 RDMA 元数据信息写入到对应的文件中
	f = open("%s/tmp_psn" % rdma_dir, "w")
	f.write(str(psn))
	f.close()

	f = open("%s/tmp_qpnum" % rdma_dir, "w")
	f.write(str(queue_pair))
	f.close()
		
	f = open("%s/tmp_memaddr" % rdma_dir, "w")
	f.write(str(memory_start))
	f.close()
		
	f = open("%s/tmp_memlen" % rdma_dir, "w")
	f.write(str(memory_length))
	f.close()
	
	f = open("%s/tmp_rkey" % rdma_dir, "w")
	f.write(str(remote_key))
	f.close()

	store_flag = True

def insertForwardingRules():
	''' 下发转发表项 (DstIP -> Egress Port) '''

	global p4, log, ipaddress, forwardingRules
	log("Inserting Forwarding rules...")
	
	# 根据目的以太网地址 (dstAddr) 来转发到对应的出口端口号 (egress port)
	for dstAddr, egrPort in forwardingRules:
		log("DstAddr: %s -> EgressPort: %i" % (dstAddr, egrPort))
		p4.SwitchIngress.tbl_forward.add_with_forward(dstaddr=dstAddr, port=egrPort)

def insertMulticastRules(duplicate_num):

	global p4, log, collectorEthertoPorts, mcRules
	log("Inserting Prep-Multicast rules...")
	
	# 获取每个 Collector 的以太网地址和对应的 egressPort
	for dstAddr, egrPort in collectorEthertoPorts:
			
		log("%s, %i, %i" % (dstAddr, egrPort, duplicate_num))
			
		# 从 mcRules 列表中查找到正确的多播组 ID (同时匹配 duplicate_num 和 egressPort)
		rule = [ r for r in mcRules if r["duplicate_num"]==duplicate_num and r["egressPort"]==egrPort]
		multicastGroupID = rule[0]["mgid"]
			
		log("Adding prep-Multicast rule %s, N = %i - %i" % (dstAddr, duplicate_num, multicastGroupID))
			
		p4.SwitchIngress.tbl_prep_multicast.add_with_prep_multiwrite(dstaddr=dstAddr, mcast_grp=multicastGroupID)


def insertPrepMemoryAddressRules():
	global p4, log, ipaddress, collectorEthertoPorts, getRDMAMetadata, bucket_size_B

	log("Inserting Prep-MemoryAddress rules...")

	# 用于存储数据包序列号的寄存器索引 (每个 Collector 对应一个寄存器索引)
	psn_reg_index = 0

	# 获取 RDMA 连接的元数据信息
	queue_pair, start_psn, memory_start, memory_length, remote_key = getRDMAMetadata()
	
	for dstAddr, _ in collectorEthertoPorts:
		
		log("Inserting memory slot rules for collector ip %s" % dstAddr)

		# 计算 Collector 中共分配了多少个插槽，即 memory_length / (Slot size in bytes)
		collector_num_slots = int(memory_length / 8)		
		
		# 填充存放数据包序列号的寄存器
		p4.SwitchEgress.CraftRDMA.reg_rdma_sequence_number.mod(f1=start_psn, register_index=psn_reg_index)
		
		log("Inserting Prep-MemoryAddress RDMA Metadata lookup rule for collector ip %s" % dstAddr)
		# 生成关于 Collector 的 RDMA 元数据信息的表项, 并将其填充到对应的表中
		p4.SwitchEgress.PrepareMemoryAddress.tbl_getRDMAMetadata.add_with_set_server_info(dstaddr=dstAddr, remote_key=remote_key, queue_pair=queue_pair, memory_address_start=memory_start, num_slots=collector_num_slots, qp_reg_index=psn_reg_index)
		
		# 递增寄存器索引 (如果有多个 Collector 的话就有用)
		psn_reg_index += 1


# (数据包复制引擎, PRE) 配置多播函数的处理逻辑已验证无误
def ConfigMulticast(duplicate_num):
	global p4, pre, log, mcRules

	log("Configuring mirroring sessions...")
	
	lastNodeID = 0
	
	for mcastGroup in mcRules:
		# 如果 duplicate_num 不等于当前 mcRule 的 duplicate_num, 则继续循环寻找
		if mcastGroup["duplicate_num"] != duplicate_num:
			continue

		# 多播组 ID
		mgid = mcastGroup["mgid"]
		# 该多播组的出口端口号
		egressPort = mcastGroup["egressPort"]
		# 哈希函数的个数
		duplicate_num = mcastGroup["duplicate_num"]
		log("Setting up multicast %i, egress port: %i, duplicate_num: %i" % (mgid, egressPort, duplicate_num))
		
		# 每个多播节点 (RID) 都是唯一的, 并且其指向的出口端口均为 egressPort
		nodeIDs = []
		log("Adding multicast nodes...")
		for _ in range(duplicate_num):
			lastNodeID += 1
			log("Creating node %i" % lastNodeID)
			pre.node.add(dev_port=[egressPort], multicast_node_id=lastNodeID)
			nodeIDs.append(lastNodeID)
		
		log("Creating the multicast group")
		# exclusion id 都是失效的
		pre.mgid.add(mgid=mgid, multicast_node_id=nodeIDs, multicast_node_l1_xid=[0]*duplicate_num, multicast_node_l1_xid_valid=[False]*duplicate_num)
	

# 入口表项下发函数已验证无误
def SetIngressTableRules(duplicate_num):
	""" 用于对入口控制阶段的表进行下发表项操作 """
	global p4, log, insertForwardingRules, insertMulticastRules

	log("--------------- Ingress Pipeline ---------------")
	insertForwardingRules()
	insertMulticastRules(duplicate_num)

# 出口表项下发函数已验证无误
def SetEgressTableRules():
	global p4, log, insertPrepMemoryAddressRules

	log("--------------- Egress  Pipeline ---------------")
	insertPrepMemoryAddressRules()
	

log("Starting configure Tofino Switch...")
# 配置 PRE 中的多播组转发规则
ConfigMulticast(duplicate_num=3)
# 配置入口表项
SetIngressTableRules(duplicate_num=3)

# 接着等待 Generator 那边发送过来的 RDMA 元数据信息然后配置出口表项
filter_expr = "udp and (src port 1111) and (dst port 5555)"
sniff(filter=filter_expr, iface="enp2s0f0", prn=storeRDMAMetadata, count=1)
if store_flag:
	SetEgressTableRules()
else:
	log("*** Cannot receive and process RDMA metadata correctly! ***")

log("Bootstrap complete")
