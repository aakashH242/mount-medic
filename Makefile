CC ?= cc
CPPFLAGS += $(shell pkg-config --cflags libntfs-3g)
CFLAGS ?= -O2 -g
CFLAGS += -Wall -Wextra -Werror -std=c11 -D_GNU_SOURCE
LDLIBS += $(shell pkg-config --libs libntfs-3g)

.PHONY: all test clean
all: build/mount-medic-probe

build/mount-medic-probe: native/probe.c
	mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDLIBS)

build/journal-fixture: tests/journal_fixture.c
	mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDLIBS)

test: all build/journal-fixture
	python3 -m unittest discover -s tests -v
	python3 tests/native_images.py

clean:
	python3 -c 'import shutil; shutil.rmtree("build", ignore_errors=True)'
