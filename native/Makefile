CFLAGS ?= -O2 -Wall -Wextra

ipod-db: ipod-db.c
	$(CC) $(CFLAGS) -o $@ $< $$(pkg-config --cflags --libs libgpod-1.0 glib-2.0)

clean:
	rm -f ipod-db
