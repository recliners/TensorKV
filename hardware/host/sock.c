#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netdb.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include "sock.h"

int sock_daemon_connect(int port)
{
	struct addrinfo hints = {
		.ai_flags = AI_PASSIVE,
		.ai_family = AF_UNSPEC,
		.ai_socktype = SOCK_STREAM
	};
	struct addrinfo *res, *t;
	char *service = NULL;
	int sockfd = -1, connfd, n = 1;

	if (asprintf(&service, "%d", port) < 0)
		return -1;

	if (getaddrinfo(NULL, service, &hints, &res) != 0) {
		free(service);
		return -1;
	}

	for (t = res; t; t = t->ai_next) {
		sockfd = socket(t->ai_family, t->ai_socktype, t->ai_protocol);
		if (sockfd < 0)
			continue;
		setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR, &n, sizeof n);
		if (bind(sockfd, t->ai_addr, t->ai_addrlen) == 0)
			break;
		close(sockfd);
		sockfd = -1;
	}

	freeaddrinfo(res);
	free(service);

	if (sockfd < 0)
		return -1;

	listen(sockfd, 1);
	connfd = accept(sockfd, NULL, 0);
	close(sockfd);
	return connfd;
}

int sock_client_connect(const char *server_name, int port)
{
	struct addrinfo hints = {
		.ai_family = AF_UNSPEC,
		.ai_socktype = SOCK_STREAM
	};
	struct addrinfo *res, *t;
	char *service = NULL;
	int sockfd = -1;

	if (asprintf(&service, "%d", port) < 0)
		return -1;

	if (getaddrinfo(server_name, service, &hints, &res) != 0) {
		free(service);
		return -1;
	}

	for (t = res; t; t = t->ai_next) {
		sockfd = socket(t->ai_family, t->ai_socktype, t->ai_protocol);
		if (sockfd < 0)
			continue;
		if (connect(sockfd, t->ai_addr, t->ai_addrlen) == 0)
			break;
		close(sockfd);
		sockfd = -1;
	}

	freeaddrinfo(res);
	free(service);
	return sockfd;
}

static int sock_recv_all(int sockfd, int size, void *buf)
{
	unsigned char *p = buf;
	while (size > 0) {
		int n = (int)read(sockfd, p, (size_t)size);
		if (n <= 0)
			return -1;
		p += n;
		size -= n;
	}
	return 0;
}

static int sock_send_all(int sockfd, int size, const void *buf)
{
	const unsigned char *p = buf;
	while (size > 0) {
		int n = (int)write(sockfd, p, (size_t)size);
		if (n <= 0)
			return -1;
		p += n;
		size -= n;
	}
	return 0;
}

int sock_sync_data(int sockfd, int is_client, int size, void *out_buf, void *in_buf)
{
	if (is_client) {
		if (sock_send_all(sockfd, size, out_buf) < 0)
			return -1;
		return sock_recv_all(sockfd, size, in_buf);
	}
	if (sock_recv_all(sockfd, size, in_buf) < 0)
		return -1;
	return sock_send_all(sockfd, size, out_buf);
}

int sock_sync_ready(int sockfd, int is_client)
{
	char cm_buf = 'a';
	return sock_sync_data(sockfd, is_client, 1, &cm_buf, &cm_buf);
}
