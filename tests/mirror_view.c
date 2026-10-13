/* Test-only read fault injection; accepts disposable regular files only. */
#define main probe_main
#include "../native/probe.c"
#undef main
#include <assert.h>

static s64 fake_offset, fake_length;
static int fake_errno, fake_changed, read_calls;
static unsigned char backing[64];
static s64 stub_seek(struct ntfs_device *dev, s64 offset, int whence) {
    (void)dev; (void)offset; (void)whence;
    if (fake_offset < 0) errno = ESPIPE;
    return fake_offset;
}
static s64 stub_read(struct ntfs_device *dev, void *buf, s64 count) {
    (void)dev; (void)count;
    read_calls++;
    if (fake_length < 0) { errno = fake_errno; return -1; }
    memcpy(buf, backing, fake_length);
    if (fake_changed && fake_length) ((unsigned char *)buf)[0] ^= 1;
    return fake_length;
}
static s64 stub_pread(struct ntfs_device *dev, void *buf, s64 count, s64 offset) {
    (void)offset;
    return stub_read(dev, buf, count);
}
static void overlap_checks(void) {
    unsigned char expected[8], replacement[8], buf[64], original[64];
    for (unsigned i = 0; i < 8; i++) { expected[i] = i + 1; replacement[i] = i + 101; }
    mirror_expected = expected; mirror_view = replacement; mirror_offset = 16; mirror_length = 8;
    unsigned partitions = 0;
    for (s64 offset = 0; offset < 40; offset++) {
        for (s64 length = 0; length <= 32; length++) {
            memset(buf, 0x55, sizeof(buf));
            for (s64 i = 0; i < length; i++) if (offset+i >= 16 && offset+i < 24) buf[i] = expected[offset+i-16];
            memcpy(original, buf, sizeof(buf));
            assert(apply_mirror_view(buf, length, offset) == 0);
            for (s64 i = 0; i < 64; i++) assert(buf[i] == ((i < length && offset+i >= 16 && offset+i < 24) ? replacement[offset+i-16] : original[i]));
            partitions++;
        }
    }
    memcpy(buf, expected, 8); buf[0] ^= 1; memcpy(original, buf, sizeof(buf));
    assert(apply_mirror_view(buf, 8, 16) == -1 && errno == ESTALE && !memcmp(buf, original, sizeof(buf)));
    memcpy(buf, expected, 8); memcpy(original, buf, sizeof(buf));
    assert(!apply_mirror_view(buf, -1, 16) && !memcmp(buf, original, sizeof(buf)));
    assert(!apply_mirror_view(buf, 8, -1) && !memcmp(buf, original, sizeof(buf)));
    assert(!apply_mirror_view(buf, 8, INT64_MAX-4) && !memcmp(buf, original, sizeof(buf)));
    struct ntfs_device_operations saved = ntfs_device_default_io_ops;
    ntfs_device_default_io_ops.seek = stub_seek;
    ntfs_device_default_io_ops.read = stub_read;
    ntfs_device_default_io_ops.pread = stub_pread;
    memcpy(backing, expected, 8); fake_offset = 16;
    for (int api = 0; api < 2; api++) {
        fake_length = 4; fake_changed = 0; device_io_failed = 0; mirror_verification_failed = 0;
        memset(buf, 0x55, sizeof(buf));
        s64 actual = api ? readonly_pread(NULL, buf, 8, 16) : readonly_read(NULL, buf, 8);
        assert(actual == 4 && !memcmp(buf, replacement, 4) && buf[4] == 0x55 && !mirror_verification_failed);
        fake_length = 0;
        assert((api ? readonly_pread(NULL, buf, 8, 16) : readonly_read(NULL, buf, 8)) == 0 && mirror_verification_failed);
        for (int failure_index = 0; failure_index < 4; failure_index++) {
            const int errors[] = { EIO, ENODEV, ENXIO, EINTR };
            fake_length = -1; fake_errno = errors[failure_index]; device_io_failed = 0; mirror_verification_failed = 0;
            assert((api ? readonly_pread(NULL, buf, 8, 16) : readonly_read(NULL, buf, 8)) == -1 && errno == fake_errno);
            assert(device_io_failed == (fake_errno != EINTR));
            assert(mirror_verification_failed == (fake_errno != EINTR));
        }
        fake_length = 8; fake_changed = 1;
        assert((api ? readonly_pread(NULL, buf, 8, 16) : readonly_read(NULL, buf, 8)) == -1 && errno == ESTALE);
    }
    fake_offset = -1; read_calls = 0;
    assert(readonly_read(NULL, buf, 8) == -1 && errno == ESPIPE && !read_calls);
    ntfs_device_default_io_ops = saved;
    mirror_view = NULL; mirror_expected = NULL;
    printf("PASS: %u overlap partitions; both callbacks preserve short/zero/failing/changed reads; seek refusal; overflow guard\n", partitions);
    return;
}

static s64 (*original_pread)(struct ntfs_device *, void *, s64, s64);
static int mode;
static s64 fault_pread(struct ntfs_device *dev, void *buf, s64 count, s64 offset) {
    if (mode && mirror_view && offset < mirror_offset && count > mirror_offset - offset) {
        fprintf(stderr, "TEST: shorten mirror read to %lld bytes\n", (long long)(mirror_offset-offset));
        count = mirror_offset-offset;
    }
    if (mirror_view && offset >= mirror_offset && offset < mirror_offset+mirror_length) {
        if (mode == 1) { fprintf(stderr, "TEST: underlying mirror EIO\n"); errno = EIO; return -1; }
        if (mode == 2) { fprintf(stderr, "TEST: underlying mirror EOF\n"); return 0; }
    }
    s64 length = original_pread(dev, buf, count, offset);
    if (mode == 3 && mirror_view && length > 0 && offset >= mirror_offset && offset < mirror_offset+mirror_length) {
        fprintf(stderr, "TEST: changed mirror byte\n"); ((unsigned char *)buf)[0] ^= 1;
    }
    return length;
}
int main(int argc, char **argv) {
    if (argc == 1) { overlap_checks(); return 0; }
    struct stat info;
    if (argc != 3 || stat(argv[2], &info) || !S_ISREG(info.st_mode)) return 2;
    mode = atoi(argv[1]);
    original_pread = ntfs_device_default_io_ops.pread;
    ntfs_device_default_io_ops.pread = fault_pread;
    char *args[] = { argv[0], argv[2], NULL };
    return probe_main(2, args);
}
