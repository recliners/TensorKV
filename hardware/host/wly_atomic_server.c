/*
 * Important notes:
 * 1. This code example can only work on 64 bit machines because the remotely accessed memory location
 * 	must be 8 Bytes aligned, and in this test we don't verify it
 * 2. In order to run the test you must first run the daemon side and only then the client side
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdint.h>
#include <inttypes.h>
#include <endian.h>
#include <byteswap.h>
#include <getopt.h>
#include <sys/time.h>
#include <infiniband/verbs.h>
#include "sock.h"
#include <json-c/json.h>
#include <byteswap.h>
#include <zlib.h>
#include <arpa/inet.h>

#define MAX_LINE_LENGTH 1000
#define MAX_POLL_CQ_TIMEOUT 2000 /* poll CQ timeout in milisec */
#define ATOMIC_MSG_SIZE 8		 /* Atomic message must be 8 bytes long */

#if __BYTE_ORDER == __LITTLE_ENDIAN
static inline uint64_t htonll(uint64_t x) { return bswap_64(x); }
static inline uint64_t ntohll(uint64_t x) { return bswap_64(x); }
#elif __BYTE_ORDER == __BIG_ENDIAN
static inline uint64_t htonll(uint64_t x) { return x; }
static inline uint64_t ntohll(uint64_t x) { return x; }
#else
#error __BYTE_ORDER is neither __LITTLE_ENDIAN nor __BIG_ENDIAN
#endif
static uint64_t size=2048;
/* structure of test parameters */
struct config_t
{
	const char *dev_name; /* IB device name */
	char *server_name;	  /* daemon host name */
	u_int32_t tcp_port;	  /* daemon TCP port */
	int ib_port;		  /* local IB port to work with */
};

/* structure to exchange data which is needed to connect the QPs */
struct cm_con_data_t
{
	uint64_t addr;	   /* Buffer address */
	uint32_t rkey;	   /* Remote key */
	uint32_t qp_num;   /* QP number */
	uint16_t lid;	   /* LID of the IB port */
	union ibv_gid gid;
} __attribute__((packed));

/* structure of needed test resources */
struct resources
{
	struct ibv_device_attr device_attr; /* Device attributes */
	struct ibv_port_attr port_attr;		/* IB port attributes */
	struct cm_con_data_t remote_props;	/* values to connect to remote side */
	struct ibv_device **dev_list;		/* device list */
	struct ibv_context *ib_ctx;			/* device handle */
	union ibv_gid gid;
	struct ibv_pd *pd;					/* PD handle */
	struct ibv_cq *cq;					/* CQ handle */
	struct ibv_qp *qp;					/* QP handle */
	struct ibv_mr *mr;					/* MR handle */
	char *buf;							/* memory buffer pointer */
	int sock;							/* TCP socket file descriptor */
};

struct config_t config = {
	"mlx5_1", /* dev_name */
	"",	  /* server_name */
	10001,	  /* tcp_port */
	1		  /* ib_port */
};

typedef struct {
    uint32_t src_ip;       
    uint32_t dst_ip;
    uint16_t src_port;
    uint16_t dst_port;
    uint8_t proto;
    uint32_t flow_size;
	uint64_t pkts_num;  
}FlowInfo;

// 每行的插槽数 = 总的内存空间大小 (Byte) / 行数 / 插槽自身的大小 (Byte)
// 总的内存空间大小在第 220 行进行修改, 行数在 table_rules.py 中修改
uint32_t rank_num_slots = 1024;

/*****************************************
 * Function: resources_init
 *****************************************/
static void resources_init(
	struct resources *res)
{
	memset(res, 0, sizeof(struct resources));

	res->sock = -1;
}

/*****************************************
 * Function: resources_create
 *****************************************/
static int resources_create(struct resources *res){
	struct ibv_qp_init_attr qp_init_attr;
	struct ibv_device *ib_dev = NULL;
	//size_t size;
	int i;
	int mr_flags = 0;
	int cq_size = 0;
	int num_devices;

	/* if client side */
	if (strlen(config.server_name) > 0)
	{
		res->sock = sock_client_connect(config.server_name, config.tcp_port);
		if (res->sock < 0)
		{
			fprintf(stderr, "failed to establish TCP connection to server %s, port %d\n",
					config.server_name, config.tcp_port);
			return -1;
		}
	}
	else
	{
		fprintf(stdout, "waiting on port %d for TCP connection\n", config.tcp_port);

		res->sock = sock_daemon_connect(config.tcp_port);
		if (res->sock < 0)
		{
			fprintf(stderr, "failed to establish TCP connection with client on port %d\n",
					config.tcp_port);
			return -1;
		}
	}

	fprintf(stdout, "TCP connection was established\n");

	fprintf(stdout, "searching for IB devices in host\n");

	/* get device names in the system */
	res->dev_list = ibv_get_device_list(&num_devices);
	if (!res->dev_list)
	{
		fprintf(stderr, "failed to get IB devices list\n");
		return 1;
	}

	/* if there isn't any IB device in host */
	if (!num_devices)
	{
		fprintf(stderr, "no IB device was found in host\n");
		return 1;
	}
	fprintf(stdout, "found %d device(s)\n", num_devices);

	/* search for the specific device we want to work with */
	for (i = 0; i < num_devices; i++)
	{
		fprintf(stdout, "device(s) name: %s\n", res->dev_list[i]->name);
		if (!strcmp(ibv_get_device_name(res->dev_list[i]), config.dev_name))
		{
			ib_dev = res->dev_list[i];
			break;
		}
	}

	/* if the device wasn't found in host */
	if (!ib_dev)
	{
		fprintf(stderr, "IB device %s wasn't found\n", config.dev_name);
		return 1;
	}

	/* get device handle */
	res->ib_ctx = ibv_open_device(ib_dev);
	if (!res->ib_ctx)
	{
		fprintf(stderr, "failed to open device %s\n", config.dev_name);
		return 1;
	}

	/* query port properties  */
	if (ibv_query_port(res->ib_ctx, config.ib_port, &res->port_attr))
	{
		fprintf(stderr, "ibv_query_port on port %u failed\n", config.ib_port);
		return 1;
	}

	if (ibv_query_gid(res->ib_ctx, config.ib_port, 0, &res->gid))
	{
		fprintf(stderr, "ibv_query_gid on port %u failed\n", config.ib_port);
		return 1;
	}

	/* allocate Protection Domain */
	res->pd = ibv_alloc_pd(res->ib_ctx);
	if (!res->pd)
	{
		fprintf(stderr, "ibv_alloc_pd failed\n");
		return 1;
	}

	/* each side will send up to one WR, so Completion Queue with 1 entry is enough */
	cq_size = 1;

	res->cq = ibv_create_cq(res->ib_ctx, cq_size, NULL, NULL, 0);
	if (!res->cq)
	{
		fprintf(stderr, "failed to create CQ with %u entries\n", cq_size);
		return 1;
	}

	/* allocate the memory buffer that will hold the data */
	//size = 2048;
	res->buf = (char *)malloc(size);
	memset(res->buf, 0, size);
	if (!res->buf){
		fprintf(stderr, "failed to malloc %Zu bytes to memory buffer\n", size);
		return 1;
	}

	/* register this memory buffer */
	mr_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_READ | IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_REMOTE_ATOMIC ;

	res->mr = ibv_reg_mr(res->pd, res->buf, size, mr_flags);
	if (!res->mr)
	{
		fprintf(stderr, "ibv_reg_mr failed with mr_flags=0x%x\n", mr_flags);
		return 1;
	}
	fprintf(stdout, "MR was registered with addr=%p, lkey=0x%x, rkey=0x%x, flags=0x%x\n",
			res->buf, res->mr->lkey, res->mr->rkey, mr_flags);

	/* create the Queue Pair */
	memset(&qp_init_attr, 0, sizeof(qp_init_attr));

	qp_init_attr.qp_type = IBV_QPT_RC;
	qp_init_attr.sq_sig_all = 1;
	qp_init_attr.send_cq = res->cq;
	qp_init_attr.recv_cq = res->cq;
	qp_init_attr.cap.max_send_wr = 32;
	qp_init_attr.cap.max_recv_wr = 32;
	qp_init_attr.cap.max_send_sge = 32;
	qp_init_attr.cap.max_recv_sge = 32;

	res->qp = ibv_create_qp(res->pd, &qp_init_attr);
	if (!res->qp)
	{
		fprintf(stderr, "failed to create QP\n");
		return 1;
	}
	fprintf(stdout, "QP was created, QP number=0x%x\n", res->qp->qp_num);

	return 0;
}

/*****************************************
 * Function: modify_qp_to_init
 *****************************************/
static int modify_qp_to_init(
	struct ibv_qp *qp)
{
	struct ibv_qp_attr attr;
	int flags;
	int rc;

	/* do the following QP transition: RESET -> INIT */
	memset(&attr, 0, sizeof(attr));

	attr.qp_state = IBV_QPS_INIT;
	attr.port_num = config.ib_port;
	attr.pkey_index = 0;
	attr.qp_access_flags = IBV_ACCESS_LOCAL_WRITE;
	if (config.server_name)
		attr.qp_access_flags |= IBV_ACCESS_REMOTE_ATOMIC | IBV_ACCESS_REMOTE_READ; /* only the client expects to get incoming Atomic operation */
	flags = IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT | IBV_QP_ACCESS_FLAGS;

	rc = ibv_modify_qp(qp, &attr, flags);
	if (rc)
	{
		fprintf(stderr, "failed to modify QP state to INIT\n");
		return rc;
	}

	return 0;
}

/*****************************************
 * Function: modify_qp_to_rtr
 *****************************************/
static int modify_qp_to_rtr(struct ibv_qp *qp, uint32_t remote_qpn, uint16_t dlid,union ibv_gid remote_gid) {
	struct ibv_qp_attr attr;
	int flags;
	int rc;

	/* do the following QP transition: INIT -> RTR */
	memset(&attr, 0, sizeof(attr));
	//in_addr_t ip = htonl(inet_addr("192.168.4.3"));

	attr.qp_state = IBV_QPS_RTR;
	attr.path_mtu = IBV_MTU_1024;
	attr.dest_qp_num = remote_qpn;
	attr.rq_psn = 0;
	attr.max_dest_rd_atomic = 8;
	attr.min_rnr_timer = 0x12;
	attr.ah_attr.is_global = 1;
    attr.ah_attr.dlid = dlid;
    attr.ah_attr.sl = 0;
	attr.ah_attr.grh.dgid = remote_gid;
    attr.ah_attr.grh.flow_label = 0;
    attr.ah_attr.grh.hop_limit = 1;
    attr.ah_attr.grh.sgid_index = 0; // 本地GID索引，需根据实际情况设置
    attr.ah_attr.grh.traffic_class = 0;
	attr.ah_attr.src_path_bits = 0;
	attr.ah_attr.port_num = config.ib_port;

	flags = IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN |
			IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER;

	rc = ibv_modify_qp(qp, &attr, flags);
	if (rc){
		fprintf(stderr, "failed to modify QP state to RTR\n");
		return rc;
	}

	return 0;
}

/*****************************************
 * Function: modify_qp_to_rts
 *****************************************/
static int modify_qp_to_rts(struct ibv_qp *qp){
	struct ibv_qp_attr attr;
	int flags;
	int rc;

	/* do the following QP transition: RTR -> RTS */
	memset(&attr, 0, sizeof(attr));

	attr.qp_state = IBV_QPS_RTS;
	attr.timeout = 0x12;
	attr.retry_cnt = 6;
	attr.rnr_retry = 0;
	attr.sq_psn = 0;
	attr.max_rd_atomic = 8;

	flags = IBV_QP_STATE | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT |
			IBV_QP_RNR_RETRY | IBV_QP_SQ_PSN | IBV_QP_MAX_QP_RD_ATOMIC;

	rc = ibv_modify_qp(qp, &attr, flags);
	if (rc){
		fprintf(stderr, "failed to modify QP state to RTS\n");
		return rc;
	}

	return 0;
}

/*****************************************
 * Function: connect_qp
 *****************************************/
static int connect_qp(struct resources *res){
	struct cm_con_data_t local_con_data, remote_con_data, tmp_con_data;
	int rc;

	/* modify the QP to init */
	rc = modify_qp_to_init(res->qp);
	if (rc){
		fprintf(stderr, "change QP state to INIT failed\n");
		return rc;
	}

	/* exchange using TCP sockets info required to connect QPs */
	local_con_data.addr = htonll((uintptr_t)res->buf);
	local_con_data.rkey = htonl(res->mr->rkey);
	local_con_data.qp_num = htonl(res->qp->qp_num);
	local_con_data.lid = htons(res->port_attr.lid);
	local_con_data.gid = res->gid;

	fprintf(stdout, "\nLocal LID        = 0x%x\n", res->port_attr.lid);

	if (sock_sync_data(res->sock, 0, sizeof(struct cm_con_data_t), &local_con_data, &tmp_con_data) < 0){
		fprintf(stderr, "failed to exchange connection data between sides\n");
		return 1;
	}

	remote_con_data.addr = ntohll(tmp_con_data.addr);
	remote_con_data.rkey = ntohl(tmp_con_data.rkey);
	remote_con_data.qp_num = ntohl(tmp_con_data.qp_num);
	remote_con_data.lid = ntohs(tmp_con_data.lid);
	remote_con_data.gid = tmp_con_data.gid;

	/* save the remote side attributes, we will need it for the post SR */
	res->remote_props = remote_con_data;

	fprintf(stdout, "Remote address   = 0x%" PRIx64 "\n", remote_con_data.addr);
	fprintf(stdout, "Remote rkey      = 0x%x\n", remote_con_data.rkey);
	fprintf(stdout, "Remote QP number = 0x%x\n", remote_con_data.qp_num);
	fprintf(stdout, "Remote LID       = 0x%x\n", remote_con_data.lid);
	fprintf(stdout, "Remote GID       = 0x%016lx:%016lx\n",
        (unsigned long)remote_con_data.gid.global.subnet_prefix,
        (unsigned long)remote_con_data.gid.global.interface_id);

	/* modify the QP to RTR */
	rc = modify_qp_to_rtr(res->qp, remote_con_data.qp_num, remote_con_data.lid, remote_con_data.gid);
	if (rc){
		fprintf(stderr, "failed to modify QP state from INIT to RTR\n");
		return rc;
	}

	/* only the daemon post SR, so only he should be in RTS
	   (the client can be moved to RTS as well)
	 */
	if (strlen(config.server_name) <= 0){
		fprintf(stdout, "QP state was change to RTR\n");
	}
	else{
		rc = modify_qp_to_rts(res->qp);
		if (rc){
			fprintf(stderr, "failed to modify QP state from RTR to RTS\n");
			return rc;
		}
		fprintf(stdout, "QP state was change to RTS\n");
	}

	return 0;
}


/* 用于释放资源, 包括 Queue Pair, Memory Region, Buffer, Completion Queue, 
   Protection Domain, IB 设备等 resources 结构体中的资源变量 */
static int resources_destroy(struct resources *res){
	int result = 0;

	if (res->qp){
		if (ibv_destroy_qp(res->qp)){
			fprintf(stderr, "failed to destroy QP\n");
			result = 1;
		}
	}

	if (res->mr){
		if (ibv_dereg_mr(res->mr)){
			fprintf(stderr, "failed to deregister MR\n");
			result = 1;
		}
	}

	if (res->buf){
		free(res->buf);
	}

	if (res->cq){
		if (ibv_destroy_cq(res->cq)){
			fprintf(stderr, "failed to destroy CQ\n");
			result = 1;
		}
	}

	if (res->pd){
		if (ibv_dealloc_pd(res->pd)){
			fprintf(stderr, "failed to deallocate PD\n");
			result = 1;
		}
	}

	if (res->ib_ctx){
		if (ibv_close_device(res->ib_ctx)){
			fprintf(stderr, "failed to close device context\n");
			result = 1;
		}
	}

	if (res->dev_list){
		ibv_free_device_list(res->dev_list);
	}
	
	if (res->sock >= 0){
		if (close(res->sock)){
			fprintf(stderr, "failed to close socket\n");
			result = 1;
		}
	}

	return result;
}

/*****************************************
 * Function: print_config
 *****************************************/
static void print_config(void){
	fprintf(stdout, " ------------------------------------------------\n");
	fprintf(stdout, " Device name                  : \"%s\"\n", config.dev_name);
	fprintf(stdout, " IB port                      : %u\n", config.ib_port);
	if (config.server_name)
		fprintf(stdout, " IP                           : %s\n", config.server_name);
	fprintf(stdout, " TCP port                     : %u\n", config.tcp_port);
	fprintf(stdout, " ------------------------------------------------\n\n");
}

/*****************************************
 * Function: usage
 *****************************************/
static void usage(const char *argv0){
	fprintf(stdout, "Usage:\n");
	fprintf(stdout, "  %s            start a server and wait for connection\n", argv0);
	fprintf(stdout, "  %s <host>     connect to server at <host>\n", argv0);
	fprintf(stdout, "\n");
	fprintf(stdout, "Options:\n");
	fprintf(stdout, "  -p, --port=<port>      listen on/connect to port <port> (default 18515)\n");
	fprintf(stdout, "  -d, --ib-dev=<dev>     use IB device <dev> (default first device found)\n");
	fprintf(stdout, "  -i, --ib-port=<port>   use port <port> of IB device (default 1)\n");
}

/* 获取接收队列对的数据包序列号 */
int getRQPSN(struct resources res){
	struct ibv_qp_attr attr;
	struct ibv_qp_init_attr init_attr;
			
	ibv_query_qp(res.qp, &attr, IBV_QP_RQ_PSN, &init_attr);
	
	return attr.rq_psn;
}

/* 用于将 RDMA 连接的元数据信息存放到当前目录下 json 文件中 */
int storeMetadata(struct resources res) {
	// Create a JSON object
	json_object *json_obj = json_object_new_object();

	// Add RDMA Metadata fields to JSON object
	json_object_object_add(json_obj, "addr", json_object_new_int64((uintptr_t)res.mr->addr));
	json_object_object_add(json_obj, "len", json_object_new_int(res.mr->length));
	json_object_object_add(json_obj, "rkey", json_object_new_int(res.mr->rkey));
	json_object_object_add(json_obj, "qpNum", json_object_new_int(res.qp->qp_num));
	// 数据包序列号 (PSN) 初始化为 0
	json_object_object_add(json_obj, "psn", json_object_new_int(0));

	// Convert JSON object to string
	const char *json_str = json_object_to_json_string_ext(json_obj, JSON_C_TO_STRING_PRETTY);

	// Create a file object and write the JSON string to this file
	FILE *file = fopen("metadata.json", "w");
	if (file == NULL) {
		// Failed to open the file, release the JSON object and return 1
		json_object_put(json_obj);
		fprintf(stderr, "Failed to open the JSON file for writing.\n");
		return 0;
	}

	fprintf(file, "%s\n", json_str);
	fclose(file);

	// Release JSON object
	json_object_put(json_obj);

	return 1;
}

/* 用于根据给定的五元组信息查询对应的估计值 */
uint64_t queryFiveTuples(struct resources res, uint32_t src_ip, uint32_t dst_ip, uint16_t src_port, uint16_t dst_port, uint8_t protocol)
{
	// 指向内存缓冲区的首地址
	char *start_ptr = res.buf;
	
	// 用于存放要计算哈希值的数据
	uint8_t buffer[14];

	uint32_t checksum = crc32(0L, Z_NULL, 0);

	// 将流的五元组信息依次拷贝到 buffer 的对应位置中 (调试可见结果)
	memcpy(buffer, &src_ip, 4);
	memcpy(buffer + 4, &dst_ip, 4);
	memcpy(buffer + 8, &src_port, 2);
	memcpy(buffer + 10, &dst_port, 2);
	memcpy(buffer + 12, &protocol, 1);

	// 用于存放最终的估计值 (初始赋值为无穷大)
	uint64_t estimate_value = UINT64_MAX;
	// 依次计算出 CMS 中每行对应的插槽的索引号, 并读取该插槽对应的数据
	for (uint8_t i = 0; i < 4; i++) {
		memset(buffer + 13, 0, 1);
        uint32_t start_slot = i * rank_num_slots;
		memcpy(buffer + 13, &i, 1);

		uint32_t slot_offset = crc32(checksum, buffer, sizeof(buffer));
		// dst_slot 是由起始插槽 start_slot 和插槽偏移量 slot_offset 相加得到的
        uint32_t dst_slot = start_slot + (slot_offset % rank_num_slots);

		// 读取该插槽对应的数据
		uint64_t parse_value = *(uint64_t *)(start_ptr + dst_slot * 8);

		// 更新最终的估计值	(如果该插槽对应的数据小于 estimate_value, 则更新 estimate_value)
		if (parse_value < estimate_value){
			estimate_value = parse_value;
		}
	}

	return estimate_value;
}

/* 用于计算关于流数量的估计的平均相对误差和平均绝对误差 */
void verifyResult(struct resources res){
	FILE *file = fopen("/home/server/wly-experiment/synthetic_flows.csv", "r");
    
	// 打开文件, 如果失败则返回错误
	if (!file) {
        perror("Unable to open file!");
		return;
    }

	char line[MAX_LINE_LENGTH];
    
    // 跳过第一行, 如果读取失败则返回错误
    if (fgets(line, sizeof(line), file) == NULL) {
        perror("Error reading header line!");
        fclose(file);
		return;
    }

	// 平均相对误差
	float average_relative_error = 0;
	// 平均绝对误差
	float average_absolute_error = 0;
	// 流的数量
	int flow_num = 0;
	// 循环读取 csv 文件中每一行的数据
	while (fgets(line, sizeof(line), file)) {
		flow_num++;
		FlowInfo flowinfo;
		if (sscanf(line, "%u,%u,%hu,%hu,%hhu,%u,%lu", 
                   &flowinfo.src_ip, &flowinfo.dst_ip, 
                   &flowinfo.src_port, &flowinfo.dst_port, 
                   &flowinfo.proto, &flowinfo.flow_size, &flowinfo.pkts_num) == 7) {
			uint64_t estimate_value = queryFiveTuples(res, htonl(flowinfo.src_ip), htonl(flowinfo.dst_ip), htons(flowinfo.src_port), htons(flowinfo.dst_port), flowinfo.proto);
			// 1. 计算相对误差, 即 |True - Estimate| / True
			// 无符号数相减需要保证结果不是负数, 因此引入判断语句
			uint64_t diff;
			if (estimate_value < flowinfo.pkts_num){
				diff = flowinfo.pkts_num - estimate_value;
			}
			else{
				diff = estimate_value - flowinfo.pkts_num;
			}
			float relative_error = (float)diff / flowinfo.pkts_num;
			average_relative_error += relative_error;
			average_absolute_error += diff;
		}
	}
	printf("The number of flows is: %d\n", flow_num);
	printf("The average relative error is: %f\n", average_relative_error / flow_num);
	printf("The average absolute error is: %f\n", average_absolute_error / flow_num);
}

/* 将内存空间中存放的数据全部重置为 0 */
void resetStorage(struct resources res){
	memset(res.buf, 0, size);
}

/* 打印内存空间中存放的数据 */
void printStorage(struct resources res){
	// start_ptr 指向 res.buf
	char *start_ptr = res.buf;
	// 每个 block 的大小为 8 字节
	int block_size = 8;
	for (int i = 0; i < size; i += block_size){
		// 每输出 8 个数换一次行
		if (i % (8 * block_size) == 0){
			fprintf(stdout, "\n");
		}
		uint64_t value = *(uint64_t *)(start_ptr + i);
		fprintf(stdout, "(%016" PRIx64 ") ", value);
	}
	printf("\n");
}

/*****************************************
******************************************
* Function: main
******************************************
*****************************************/
int main(int argc, char *argv[])
{
	struct resources res;
	//uint64_t client_data, daemon_data, data, comp_add_operand, swap_operand = 0;
	//enum ibv_wr_opcode opcode;
	int test_result = 1;

	/* parse the command line parameters */
	while (1){
		int c;
		static struct option long_options[] = {
			{.name = "port", .has_arg = 1, .val = 'p'},
			{.name = "ib-dev", .has_arg = 1, .val = 'd'},
			{.name = "ib-port", .has_arg = 1, .val = 'i'},
			{.name = NULL, .has_arg = 0, .val = '\0'}};

		c = getopt_long(argc, argv, "p:d:i", long_options, NULL);
		if (c == -1)
			break;

		switch (c){
		case 'p':
			config.tcp_port = strtoul(optarg, NULL, 0); // 解析字符串为无符号长整型
			break;
		case 'd':
			config.dev_name = optarg;
			break;
		case 'i':
			config.ib_port = strtoul(optarg, NULL, 0);
			if (config.ib_port < 0){
				usage(argv[0]);
				return 1;
			}
			break;
		default:
			usage(argv[0]);
			return 1;
		}
	}

	/* parse the last parameter (if exists) as the server name */
	if (optind == argc - 1){
		config.server_name = argv[optind];
	}
	else if (optind < argc){
		usage(argv[0]);
		return 1;
	}

	/* print the used parameters for info*/
	print_config();

	/* init all of the resources, so cleanup will be easy */
	resources_init(&res);

	/* create resources before using them */
	if (resources_create(&res)){
		fprintf(stderr, "failed to create resources\n");
		goto cleanup;
	}

	/* connect the QPs */
	if (connect_qp(&res)){
		fprintf(stderr, "failed to connect QPs\n");
		goto cleanup;
	}

	/* The following code is what CMS needed */
	// Write the RDMA metadata to a JSON file
	if (!storeMetadata(res)){
		fprintf(stderr, "Failed to write RDMA metadata to file.\n");
	}
	else{
		printf("Write to RDMA metadata to file successfully.\n");
	}
	
	// Send RDMA Metadata to Tofino Switch
	if (system("python3 read_metadata.py")){
		fprintf(stderr, "Failed to send the RDMA metadata to Tofino Switch.\n");
	}
	else{
		printf("Send the RDMA metadata to Tofino Switch successfully.\n");
	}

    printf("Press quit to disconnect...\n");
    while (1){
		char input[100];
		printf("Enter command: ");
		if (scanf("%s", input) == 1){
			// 如果输入为 "verify"，则执行验证结果操作
			if (strcmp(input, "verify") == 0){
				verifyResult(res);
			}
			// 如果输入为 "query"，则执行查询操作
			else if (strcmp(input, "query") == 0){
				uint64_t est = queryFiveTuples(res, htonl(3232235777), htonl(3232235778), htons(1540), htons(5451), 17);
				printf("The estimated value of this flow is: %lu\n", est);
			}
			// 如果输入为 "quit"，则退出循环
			else if (strcmp(input, "quit") == 0){
				printf("Disconnecting...\n");
				break;
			}
			// 如果输入为 "print"，则打印当前内存中存放的数据
			else if (strcmp(input, "print") == 0){
				printStorage(res);
				fprintf(stdout, "Current PSN is %d\n", getRQPSN(res));
			}
			else if (strcmp(input, "reset") == 0){
				resetStorage(res);
			}

		}
		else{
			printf("Failed to read input data\n");
		}
		
    }
	
	test_result = 0;
	

cleanup:
	if (resources_destroy(&res)){
		fprintf(stderr, "failed to destroy resources\n");
		test_result = 1;
	}

	fprintf(stdout, "\ntest status is %d\n", test_result);

	return test_result;
}
