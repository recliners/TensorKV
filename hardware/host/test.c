#include <stdint.h>
#include <rte_cycles.h>
#include <rte_ethdev.h>
#include <rte_mbuf.h>

/*
 * Token-bucket TX pacing for DPDK. `running` is cleared by the control
 * thread when the test should stop.
 */
void tkv_token_tx_burst(uint16_t port_id, uint16_t queue_id,
			struct rte_mbuf *mbuf, volatile int *running)
{
	uint64_t tokens = 0;
	const uint64_t token_rate = 1000000;
	uint64_t last_time = rte_rdtsc();

	while (*running) {
		uint64_t now = rte_rdtsc();
		tokens += (now - last_time) * token_rate / rte_get_tsc_hz();
		last_time = now;
		if (tokens >= 1) {
			rte_eth_tx_burst(port_id, queue_id, &mbuf, 1);
			tokens--;
		}
	}
}
