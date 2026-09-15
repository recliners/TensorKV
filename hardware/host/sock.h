#ifndef TKV_SOCK_H
#define TKV_SOCK_H

int sock_daemon_connect(int port);
int sock_client_connect(const char *server_name, int port);
int sock_sync_data(int sockfd, int is_client, int size, void *out_buf, void *in_buf);
int sock_sync_ready(int sockfd, int is_client);

#endif
