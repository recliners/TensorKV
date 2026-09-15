CC ?= gcc
CFLAGS ?= -O2 -Wall -Wextra

.PHONY: all clean

all: atomic_client wly_atomic_server

atomic_client: atomic_client.c sock.c
	$(CC) $(CFLAGS) -o $@ $^ -libverbs

wly_atomic_server: wly_atomic_server.c sock.c
	$(CC) $(CFLAGS) -o $@ $^ -libverbs -ljson-c -lz

clean:
	rm -f atomic_client wly_atomic_server
