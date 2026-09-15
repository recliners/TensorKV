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
#include <arpa/inet.h>
#include <infiniband/verbs.h>
#include "sock.h"

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
	uint64_t addr;	 /* Buffer address */
	uint32_t rkey;	 /* Remote key */
	uint32_t qp_num; /* QP number */
	uint16_t lid;	 /* LID of the IB port */
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
	struct ibv_pd *pd; /* PD handle */
	struct ibv_cq *cq; /* CQ handle */
	struct ibv_qp *qp; /* QP handle */
	struct ibv_mr *mr; /* MR handle */
	char *buf;		   /* memory buffer pointer */
	int sock;		   /* TCP socket file descriptor */
};

struct keywriteEntry
{
	uint32_t checksum;
	uint32_t data;

	// uint32_t offset;
};

struct config_t config = {
	"rocep5s0f1",  /* dev_name */
	"192.168.3.3", /* server_name */
	10001,		   /* tcp_port */
	1			   /* ib_port */
};

static int poll_cq(
	struct resources *res)
{
	struct ibv_wc wc;
	int ret;

	/* poll the completion for a while before giving up of doing it .. */
	while ((ret = ibv_poll_cq(res->cq, 1, &wc)) == 0)
	{
		// Poll until we get a completion
	}
	if (ret < 0)
	{
		fprintf(stderr, "Failed to poll completion\n");
		return 1;
	}

	fprintf(stdout, "completion was found in CQ with status 0x%x\n", wc.status);

	/* check the completion status (here we don't care about the completion opcode */
	if (wc.status != IBV_WC_SUCCESS)
	{
		fprintf(stderr, "got bad completion with status: 0x%x, vendor syndrome: 0x%x\n",
				wc.status, wc.vendor_err);
		return 1;
	}

	return 0;
}


static int post_read_write_send(
	struct resources *res,
	enum ibv_wr_opcode opcode)
{
	struct ibv_send_wr sr;
	struct ibv_sge sge;
	struct ibv_send_wr *bad_wr;
	int rc;

	/* prepare the scatter/gather entry */
	memset(&sge, 0, sizeof(sge));

	sge.addr = (uintptr_t)res->buf;
	sge.length = 16;
	sge.lkey = res->mr->lkey;
	printf("The send uint64_t number is: %lu\n", *(uint64_t *)res->buf);

	/* prepare the SR */
	memset(&sr, 0, sizeof(sr));

	// sr.next = NULL;
	sr.wr_id = 0;
	sr.sg_list = &sge;
	sr.num_sge = 1;
	sr.opcode = opcode;
	sr.send_flags = IBV_SEND_SIGNALED;

	sr.wr.rdma.remote_addr = res->remote_props.addr;
	sr.wr.rdma.rkey = res->remote_props.rkey;
	fprintf(stdout, "Remote address   = 0x%" PRIx64 "\n", res->remote_props.addr);
	fprintf(stdout, "Remote rkey      = 0x%x\n", res->remote_props.rkey);

	/* there is a Receive Request in the responder side, so we won't get any into RNR flow */
	rc = ibv_post_send(res->qp, &sr, &bad_wr);
	if (rc)
	{
		fprintf(stderr, "failed to post SR\n");
		return 1;
	}
	fprintf(stdout, "Send Request was posted\n");

	return 0;
}

static int do_read_write_operation(
	struct resources *res,
	enum ibv_wr_opcode opcode,
	uint64_t data)
{
	/* set the value in the buffer.
	the data set in the daemon's buffer won't be used but it's set in order to show that it's being replaced by the atomic operation */
	if (opcode == IBV_WR_RDMA_WRITE)
		*(uint64_t *)res->buf = data;

	/* let the daemon post the SR */
	if (post_read_write_send(res, opcode))
	{
		fprintf(stderr, "failed to post SR\n");
		return 1;
	}

	/* poll for completion to make sure the daemon finished the atomic operation and that the data was written */
	if (poll_cq(res))
	{
		fprintf(stderr, "poll completion failed\n");
		return 1;
	}

	return 0;
}

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
static int resources_create(
	struct resources *res,
	int buffer_size)
{
	struct ibv_qp_init_attr qp_init_attr;
	struct ibv_device *ib_dev = NULL;
	size_t size = buffer_size;
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
		// fprintf(stdout, "device(s) name: %s\n", res->dev_list[i]);
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
	// size = 1024;
	res->buf = (char *)malloc(size);
	if (!res->buf)
	{
		fprintf(stderr, "failed to malloc %Zu bytes to memory buffer\n", size);
		return 1;
	}

	/* register this memory buffer */
	mr_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_READ | IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_REMOTE_ATOMIC;

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
	qp_init_attr.cap.max_send_wr = 2048;
	qp_init_attr.cap.max_recv_wr = 2048;
	qp_init_attr.cap.max_send_sge = 2048;
	qp_init_attr.cap.max_recv_sge = 2048;

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
	attr.qp_access_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_READ | IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_REMOTE_ATOMIC;
	// if (config.server_name)
	//	attr.qp_access_flags |= IBV_ACCESS_REMOTE_ATOMIC | IBV_ACCESS_REMOTE_READ; /* only the client expects to get incoming Atomic operation */
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
static int modify_qp_to_rtr(
	struct ibv_qp *qp,
	uint32_t remote_qpn,
	uint16_t dlid,
	union ibv_gid remote_gid)
{
	struct ibv_qp_attr attr;
	int flags;
	int rc;

	/* do the following QP transition: INIT -> RTR */
	memset(&attr, 0, sizeof(attr));

	attr.qp_state = IBV_QPS_RTR;
	attr.path_mtu = IBV_MTU_1024;
	attr.dest_qp_num = remote_qpn;
	attr.rq_psn = 0;
	attr.max_dest_rd_atomic = 1;
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
	attr.ah_attr.port_num = config.ib_port; // 1

	flags = IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN |
			IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER;

	rc = ibv_modify_qp(qp, &attr, flags);
	if (rc)
	{
		fprintf(stderr, "failed to modify QP state to RTR\n");
		return rc;
	}

	return 0;
}

/*****************************************
 * Function: modify_qp_to_rts
 *****************************************/
static int modify_qp_to_rts(
	struct ibv_qp *qp)
{
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
	attr.max_rd_atomic = 1;

	flags = IBV_QP_STATE | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT |
			IBV_QP_RNR_RETRY | IBV_QP_SQ_PSN | IBV_QP_MAX_QP_RD_ATOMIC;

	rc = ibv_modify_qp(qp, &attr, flags);
	if (rc)
	{
		fprintf(stderr, "failed to modify QP state to RTS\n");
		return rc;
	}

	return 0;
}

/*****************************************
 * Function: connect_qp
 *****************************************/
static int connect_qp(
	struct resources *res)
{
	struct cm_con_data_t local_con_data, remote_con_data, tmp_con_data;
	int rc;

	/* modify the QP to init */
	rc = modify_qp_to_init(res->qp);
	if (rc)
	{
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

	if (sock_sync_data(res->sock, 1, sizeof(struct cm_con_data_t), &local_con_data, &tmp_con_data) < 0)
	{
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
	if (rc)
	{
		fprintf(stderr, "failed to modify QP state from INIT to RTR\n");
		return rc;
	}

	/* only the daemon post SR, so only he should be in RTS
	   (the client can be moved to RTS as well)
	 */
	if (strlen(config.server_name) < 0)
		fprintf(stdout, "QP state was change to RTR\n");
	else
	{
		rc = modify_qp_to_rts(res->qp);
		if (rc)
		{
			fprintf(stderr, "failed to modify QP state from RTR to RTS\n");
			return rc;
		}

		fprintf(stdout, "QP state was change to RTS\n");
	}

	return 0;
}

/*****************************************
 * Function: resources_destroy
 *****************************************/
static int resources_destroy(
	struct resources *res)
{
	int test_result = 0;

	if (res->qp)
	{
		if (ibv_destroy_qp(res->qp))
		{
			fprintf(stderr, "failed to destroy QP\n");
			test_result = 1;
		}
	}

	if (res->mr)
	{
		if (ibv_dereg_mr(res->mr))
		{
			fprintf(stderr, "failed to deregister MR\n");
			test_result = 1;
		}
	}

	if (res->buf)
		free(res->buf);

	if (res->cq)
	{
		if (ibv_destroy_cq(res->cq))
		{
			fprintf(stderr, "failed to destroy CQ\n");
			test_result = 1;
		}
	}

	if (res->pd)
	{
		if (ibv_dealloc_pd(res->pd))
		{
			fprintf(stderr, "failed to deallocate PD\n");
			test_result = 1;
		}
	}

	if (res->ib_ctx)
	{
		if (ibv_close_device(res->ib_ctx))
		{
			fprintf(stderr, "failed to close device context\n");
			test_result = 1;
		}
	}

	if (res->dev_list)
		ibv_free_device_list(res->dev_list);

	if (res->sock >= 0)
	{
		if (close(res->sock))
		{
			fprintf(stderr, "failed to close socket\n");
			test_result = 1;
		}
	}

	return test_result;
}

/*****************************************
 * Function: print_config
 *****************************************/
static void print_config(void)
{
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
static void usage(const char *argv0)
{
	fprintf(stdout, "Usage:\n");
	fprintf(stdout, "  %s            start a server and wait for connection\n", argv0);
	fprintf(stdout, "  %s <host>     connect to server at <host>\n", argv0);
	fprintf(stdout, "\n");
	fprintf(stdout, "Options:\n");
	fprintf(stdout, "  -p, --port=<port>      listen on/connect to port <port> (default 18515)\n");
	fprintf(stdout, "  -d, --ib-dev=<dev>     use IB device <dev> (default first device found)\n");
	fprintf(stdout, "  -i, --ib-port=<port>   use port <port> of IB device (default 1)\n");
}

/*****************************************
******************************************
* Function: main
******************************************
*****************************************/
int main(int argc, char *argv[])
{
	struct resources res;
	uint64_t data;
	enum ibv_wr_opcode opcode;
	int test_result = 1;

	/* parse the command line parameters */
	while (1)
	{
		int c;

		static struct option long_options[] = {
			{.name = "port", .has_arg = 1, .val = 'p'},
			{.name = "ib-dev", .has_arg = 1, .val = 'd'},
			{.name = "ib-port", .has_arg = 1, .val = 'i'},
			{.name = NULL, .has_arg = 0, .val = '\0'}};

		c = getopt_long(argc, argv, "p:d:i", long_options, NULL);
		if (c == -1)
			break;

		switch (c)
		{
		case 'p':
			config.tcp_port = strtoul(optarg, NULL, 0); // 解析字符串为无符号长整型
			break;

		case 'd':
			config.dev_name = optarg;
			break;

		case 'i':
			config.ib_port = strtoul(optarg, NULL, 0);
			if (config.ib_port < 0)
			{
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
	if (optind == argc - 1)
		config.server_name = argv[optind];
	else if (optind < argc)
	{
		usage(argv[0]);
		return 1;
	}

	/* print the used parameters for info*/
	print_config();

	/* init all of the resources, so cleanup will be easy */
	resources_init(&res);

	/* create resources before using them */
	if (resources_create(&res,2048))
	{
		fprintf(stderr, "failed to create resources\n");
		goto cleanup;
	}

	/* connect the QPs */
	if (connect_qp(&res))
	{
		fprintf(stderr, "failed to connect QPs\n");
		goto cleanup;
	}

	// read and write
	opcode = IBV_WR_RDMA_WRITE;
	data = 100;

	/* swap_operand isn't being used, so we didn't set it here */
	if (do_read_write_operation(&res, opcode, data))
	{
		fprintf(stderr, "failed in do_write_operation\n");
		goto cleanup;
	}

	fprintf(stdout, "----------- WRITE -------------\n");
	fprintf(stdout, "The writed value was 0x%" PRIx64 "\n", data);
	fprintf(stdout, "---------------------------------------\n");

	opcode = IBV_WR_RDMA_READ;
	// data = 100;
	memset(&res.buf, 0, sizeof(res.buf));

	/* swap_operand isn't being used, so we didn't set it here */
	if (do_read_write_operation(&res, opcode, 0))
	{
		fprintf(stderr, "failed in do_read_operation\n");
		goto cleanup;
	}
	data = *(uint64_t *)res.buf;

	fprintf(stdout, "----------- READ -------------\n");
	fprintf(stdout, "The read value was 0x%" PRIx64 "\n", data);
	fprintf(stdout, "---------------------------------------\n");

	/* sync to make sure the data was read before posting new data */
	if (sock_sync_ready(res.sock, !config.server_name))
	{
		fprintf(stderr, "sync after the data was read\n");
		return 1;
	}

	test_result = 0;

cleanup:
	if (resources_destroy(&res))
	{
		fprintf(stderr, "failed to destroy resources\n");
		test_result = 1;
	}

	fprintf(stdout, "\ntest status is %d\n", test_result);

	return test_result;
}
