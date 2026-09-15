#ifndef TKV_TX_H
#define TKV_TX_H

#include <stdint.h>
#include <rte_mbuf.h>

#ifndef BURST
#define BURST 32
#endif

#ifndef TX_FRAME_LEN
#define TX_FRAME_LEN 128
#endif

#ifndef WORK_PORT
#define WORK_PORT 0
#endif

typedef struct {
	uint32_t len;
} txSize;

struct rte_mbuf *set_mbuf(uint16_t frame_len, struct rte_mempool *mempool);
int portTx(void *arg);

#endif
