uint64_t tokens = 0;
const uint64_t token_rate = 1000000;
// 每秒生成的令牌数
uint64_t last_time = rte_rdtsc();
while (running)
{
    uint64_t now = rte_rdtsc();
    tokens += (now - last_time) * token_rate / rte_get_tsc_hz();
    last_time = now;
    if (tokens >= 1)
    {
        rte_eth_tx_burst(port_id, queue_id, &mbuf, 1);
        tokens--;
    }
}
