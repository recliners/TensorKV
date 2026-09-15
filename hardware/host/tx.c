
#include <rte_eal.h>
#include <rte_mbuf.h>
#include <rte_ether.h>
#include <rte_ip.h>
#include <rte_udp.h>
#include <rte_ethdev.h>
#include <rte_malloc.h>
#include <unistd.h>

#include "util.h"
#include "tx.h"


struct custom_int_hdr
{
    uint16_t type;   // 协议类型
    uint16_t length; // INT头长度，包括type和length字段
};
// 生成随机MAC地址
void generate_random_mac(uint8_t *mac)
{
    for (int i = 0; i < 6; ++i)
    {
        mac[i] = (uint8_t)(rte_rand() & 0xff);
    }
    // 确保本地管理地址位（第二字节的第7位）为2，符合非组织唯一地址的规则
    mac[1] |= 2;
}

unsigned int generate_random_ip()
{
    unsigned char byte1 = 0x01;         // First byte is fixed as 0x01
    unsigned char byte2 = rand() % 256; // Second byte is random
    unsigned char byte3 = 0xf1;         // Third byte is fixed as 0xf1
    unsigned char byte4 = rand() % 256; // Fourth byte is random
    unsigned int ip = (byte1 << 24) | (byte2 << 16) | (byte3 << 8) | byte4;
    return ip;
}

#define IPv4(a, b, c, d) ((uint32_t)((a & 0xFF) << 24) | ((b & 0xFF) << 16) | ((c & 0xFF) << 8) | (d & 0xFF))
// 定义一个新的 UDP 头部结构，包含 2 bit 的 opcode 字段
struct indq_base_hdr
{
    // 1 写 2读 3读返回
    uint8_t opcode : 2;
};

// 定义一个新的结构来包含 UDP 负载（key 和 value）
struct indq_payload
{
    uint32_t key;
    uint32_t value;
};
// 创建并初始化数据包的函数
struct rte_mbuf *
set_mbuf(uint16_t frame_len, struct rte_mempool *mempool)
{

    // 从内存池中分配一个mbuf
    struct rte_mbuf *mbuf = rte_pktmbuf_alloc(mempool);
    if (mbuf == NULL)
        return NULL;

    // dpdk预存地址
    mbuf->pkt_len = frame_len;
    mbuf->data_len = frame_len;

    void *pktdata = rte_pktmbuf_mtod(mbuf, void *);

    // Ethernet 以太网头部
    struct rte_ether_hdr *hdr_ether = (struct rte_ether_hdr *)pktdata;

    // 设置源地址和目标地址

    memset(&hdr_ether->src_addr.addr_bytes, 0, sizeof(struct rte_ether_addr));
    memset(&hdr_ether->dst_addr.addr_bytes, 0, sizeof(struct rte_ether_addr));

    // TODO 改成86 87的mac
    uint8_t src_mac[6] = {0x08, 0xc0, 0xeb, 0x24, 0x68, 0x6b};
    //  目的MAC地址为 11:22:33:44:55:66
    uint8_t dst_mac[6] = {0x08, 0xc0, 0xeb, 0x24, 0x7b, 0x8b};

    // 然后复制这些值到以太网头中
    memcpy(hdr_ether->src_addr.addr_bytes, src_mac, 6);
    memcpy(hdr_ether->dst_addr.addr_bytes, dst_mac, 6);

    // 设置以太网类型为IPv4
    hdr_ether->ether_type = htons(RTE_ETHER_TYPE_IPV4);

    // IPv4
    struct rte_ipv4_hdr *hdr_ipv4 = (struct rte_ipv4_hdr *)(hdr_ether + 1);

    hdr_ipv4->version_ihl = 0x45;
    hdr_ipv4->type_of_service = 0;
    hdr_ipv4->total_length = htons(frame_len - sizeof(struct rte_ether_hdr));
    hdr_ipv4->packet_id = 0;
    hdr_ipv4->fragment_offset = 0;
    hdr_ipv4->time_to_live = 64;
    hdr_ipv4->next_proto_id = IPPROTO_UDP;
    // hdr_ipv4->src_addr = 0x01f10606;
    // hdr_ipv4->dst_addr = 0x02f10606;
    hdr_ipv4->src_addr = rte_cpu_to_be_32(IPv4(192, 168, 4, 3));
    hdr_ipv4->dst_addr = rte_cpu_to_be_32(IPv4(192, 168, 3, 3));

    hdr_ipv4->hdr_checksum = 0;
    hdr_ipv4->hdr_checksum = rte_ipv4_cksum(hdr_ipv4);

    // UDP
    struct rte_udp_hdr *hdr_udp = (struct rte_udp_hdr *)(hdr_ipv4 + 1);
    hdr_udp->src_port = htons(50002);
    hdr_udp->dst_port = htons(50002);
    hdr_udp->dgram_len = htons(sizeof(struct indq_base_hdr) + sizeof(struct indq_payload));
    // 重新计算UDP校验和，现在需要考虑header
    hdr_udp->dgram_cksum = 0;
    hdr_udp->dgram_cksum = rte_ipv4_udptcp_cksum(hdr_ipv4, hdr_udp);

    struct indq_base_hdr *hdr_indq = (struct indq_base_hdr *)(hdr_udp + 1);
    hdr_indq->opcode = 0b01; // opcode 1写2读3 read response

    // UDP 负载（key 和 value）
    struct indq_payload *payload = (struct indq_payload *)(hdr_indq + 1);
    payload->key = htonl(0x00000001);   // 示例 key 值（32 bit）1
    payload->value = htonl(0x00000064); // 示例 value 值（32 bit）100

    return mbuf;
}

#if DEBUG
// 发送端口数据包的函数
int portTx(void *arg)
{
#else
int portTx(__attribute__((unused)) void *arg)
{
#endif

    // 创建一个内存池用于发送数据包
    struct rte_mempool *tx_mempool = rte_pktmbuf_pool_create("buf", 4095, 0, 0, RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
#if DEBUG
    txSize *ts = (txSize *)arg;
    printf("portTx(): ts->len: %u\n", ts->len);
#endif
    struct rte_mbuf *tx[BURST];
    int i;

    // 初始化要发送的数据包
    for (i = 0; i < BURST; i++)
    {
#if DEBUG
        tx[i] = set_mbuf(ts->len, tx_mempool);
#else
        tx[i] = set_mbuf(TX_FRAME_LEN, tx_mempool);
#endif
    }
    uint64_t tokens = 1;
    const uint64_t token_rate = 1000000;
    // 每秒生成的令牌数
    uint64_t last_time = rte_rdtsc();
    // 无限循环发送数据包
    // while(1)
    for (i = 0; i < 10; i++)
    {
        uint64_t now = rte_rdtsc();
        tokens += (now - last_time) * token_rate / rte_get_tsc_hz();
        last_time = now;
        if (tokens >= 1){
            rte_eth_tx_burst(WORK_PORT, 0, tx, BURST);
            tokens--;
        }
        // 分配MBUF
        usleep(0);
    }
    // 释放mbuf
    for (i = 0; i < BURST; i++)
    {
        rte_free(tx[i]);
    }
    // 释放内存池
    rte_free(tx_mempool);

    return 0;
}
