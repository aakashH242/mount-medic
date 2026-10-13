/* MIT. Uses the distribution's libntfs-3g; no NTFS utility source is vendored. */
#include <errno.h>
#include <stdint.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>
#include <linux/fs.h>
#include <linux/hdreg.h>
#include <ntfs-3g/volume.h>
#include <ntfs-3g/device.h>
#include <ntfs-3g/device_io.h>
#include <ntfs-3g/attrib.h>
#include <ntfs-3g/inode.h>
#include <ntfs-3g/logfile.h>
#include <ntfs-3g/logging.h>
#include <ntfs-3g/mst.h>

static int device_io_failed;
static int mirror_verification_failed;
static unsigned char *mirror_view, *mirror_expected;
static s64 mirror_offset, mirror_length;

static int apply_mirror_view(void *buf, s64 length, s64 offset)
{
    if (!mirror_view || length <= 0 || offset < 0 || offset > INT64_MAX - length)
        return 0;
    s64 start = offset > mirror_offset ? offset : mirror_offset;
    s64 end = offset + length;
    if (end > mirror_offset + mirror_length) end = mirror_offset + mirror_length;
    if (start >= end) return 0;
    unsigned char *target = (unsigned char *)buf + start - offset;
    if (memcmp(target, mirror_expected + start - mirror_offset, end - start)) {
        fprintf(stderr, "Metadata mirror changed during read-only verification.\n");
        mirror_verification_failed = 1;
        errno = ESTALE;
        return -1;
    }
    memcpy(target, mirror_view + start - mirror_offset, end - start);
    return 0;
}

static s64 readonly_read(struct ntfs_device *dev, void *buf, s64 count)
{
    s64 offset = ntfs_device_default_io_ops.seek(dev, 0, SEEK_CUR);
    if (mirror_view && offset < 0) {
        mirror_verification_failed = 1;
        return -1;
    }
    s64 length = ntfs_device_default_io_ops.read(dev, buf, count);
    if (mirror_view && count > 0 && (length == 0 || (length < 0 && errno != EINTR)))
        mirror_verification_failed = 1;
    if (length < 0 && (errno == EIO || errno == ENODEV || errno == ENXIO))
        device_io_failed = 1;
    return apply_mirror_view(buf, length, offset) ? -1 : length;
}

static s64 readonly_pread(struct ntfs_device *dev, void *buf, s64 count, s64 offset)
{
    s64 length = ntfs_device_default_io_ops.pread(dev, buf, count, offset);
    if (mirror_view && count > 0 && (length == 0 || (length < 0 && errno != EINTR)))
        mirror_verification_failed = 1;
    if (length < 0 && (errno == EIO || errno == ENODEV || errno == ENXIO))
        device_io_failed = 1;
    return apply_mirror_view(buf, length, offset) ? -1 : length;
}

/* Enforce read-only I/O even if a future library calls a mutating callback. */
static int readonly_open(struct ntfs_device *dev, int flags)
{
    if ((flags & O_ACCMODE) != O_RDONLY) {
        errno = EROFS;
        return -1;
    }
    return ntfs_device_default_io_ops.open(dev, flags);
}

static s64 deny_write(struct ntfs_device *dev, const void *buf, s64 count)
{
    (void)dev; (void)buf; (void)count;
    errno = EROFS;
    return -1;
}

static s64 deny_pwrite(struct ntfs_device *dev, const void *buf, s64 count, s64 offset)
{
    (void)offset;
    return deny_write(dev, buf, count);
}

static int readonly_ioctl(struct ntfs_device *dev, unsigned long request, void *arg)
{
    switch (request) {
    case BLKGETSIZE: case BLKGETSIZE64: case BLKSSZGET:
    case BLKROGET: case BLKBSZGET: case HDIO_GETGEO:
        return ntfs_device_default_io_ops.ioctl(dev, request, arg);
    default:
        errno = EROFS;
        return -1;
    }
}

static int result(const char *state, int complete)
{
    printf("{\"schema\":1,\"state\":\"%s\",\"complete\":%s,\"stale_volume_mirror\":%s}\n",
           state, complete ? "true" : "false", mirror_view ? "true" : "false");
    free(mirror_view);
    free(mirror_expected);
    return 0;
}

static const char *failure(int error)
{
    if (device_io_failed) return "io_failure";
    if (error == EACCES || error == EPERM) return "insufficient_access";
    if (error == EIO) return "io_or_corruption";
    if (error == EBUSY) return "busy";
    return "unknown";
}

/* Locate one ordinary resident volume-information attribute, without trusting offsets. */
static unsigned volume_flags_offset(MFT_RECORD *record, unsigned size)
{
    unsigned used = le32_to_cpu(record->bytes_in_use);
    unsigned offset = le16_to_cpu(record->attrs_offset);
    unsigned usa = le16_to_cpu(record->usa_ofs);
    unsigned count = le16_to_cpu(record->usa_count);
    unsigned found = 0;
    if (!ntfs_is_mft_record(record->magic) || record->flags != MFT_RECORD_IN_USE ||
        record->base_mft_record || le32_to_cpu(record->mft_record_number) != FILE_Volume ||
        le32_to_cpu(record->bytes_allocated) != size || used > size || used < sizeof(*record) ||
        (used & 7) || (offset & 7) || usa < sizeof(*record) || count != size / 512 + 1 ||
        usa > offset || count * 2 > offset - usa) return 0;
    while (offset <= used && used - offset >= sizeof(le32)) {
        ATTR_RECORD *attr = (void *)((unsigned char *)record + offset);
        if (attr->type == AT_END) return found;
        if (used - offset < 24) return 0;
        unsigned length = le32_to_cpu(attr->length);
        if (length < 24 || (length & 7) || length > used - offset ||
            attr->type == AT_ATTRIBUTE_LIST) return 0;
        if (attr->type == AT_VOLUME_INFORMATION) {
            unsigned value = le16_to_cpu(attr->value_offset);
            if (found || attr->non_resident || attr->name_length || attr->flags ||
                value < 24 || value > length || sizeof(VOLUME_INFORMATION) > length - value ||
                le32_to_cpu(attr->value_length) != sizeof(VOLUME_INFORMATION)) return 0;
            VOLUME_INFORMATION *info = (void *)((unsigned char *)attr + value);
            if (info->major_ver != 3 || info->minor_ver != 1) return 0;
            found = offset + value + offsetof(VOLUME_INFORMATION, flags);
        }
        offset += length;
    }
    return 0;
}

static int prepare_mirror_view(const char *path, struct ntfs_device_operations *io)
{
    struct ntfs_device *dev = ntfs_device_alloc(path, 0, io, NULL);
    ntfs_volume *vol = dev ? ntfs_volume_startup(dev, NTFS_MNT_RDONLY) : NULL;
    unsigned char *primary = NULL, *mirror = NULL, *replacement = NULL, *expected = NULL;
    s64 physical = 0;
    int valid = 0;
    if (!vol) {
        if (dev) ntfs_device_free(dev);
        return 0;
    }
    unsigned size = vol->mft_record_size;
    /* shortcut: four records in one mirror cluster; map runlists for larger layouts. */
    if (vol->mftmirr_size != 4 || size < 512 || size > 65536 || size * 4 > vol->cluster_size ||
        vol->mftmirr_lcn < 0 || vol->mftmirr_lcn > (INT64_MAX - size * 4) / vol->cluster_size)
        goto done;
    unsigned span = size * 4;
    primary = malloc(span); mirror = malloc(span);
    replacement = malloc(size); expected = malloc(size);
    if (!primary || !mirror || !replacement || !expected ||
        ntfs_attr_pread(vol->mft_na, 0, span, primary) != span ||
        ntfs_attr_pread(vol->mftmirr_na, 0, span, mirror) != span) goto done;
    memcpy(replacement, primary + size * FILE_Volume, size);
    memcpy(expected, mirror + size * FILE_Volume, size);
    for (unsigned index = 0; index < 4; index++) {
        if (ntfs_mst_post_read_fixup((void *)(primary + index * size), size) ||
            ntfs_mst_post_read_fixup((void *)(mirror + index * size), size)) goto done;
    }
    MFT_RECORD *a = (void *)(primary + size * FILE_Volume);
    MFT_RECORD *b = (void *)(mirror + size * FILE_Volume);
    unsigned flag = volume_flags_offset(a, size);
    if (!flag || volume_flags_offset(b, size) != flag ||
        memcmp((unsigned char *)a + flag, "\1\0", 2) ||
        memcmp((unsigned char *)b + flag, "\0\0", 2)) goto done;
    memcpy((unsigned char *)b + flag, (unsigned char *)a + flag, 2);
    memcpy((unsigned char *)b + le16_to_cpu(b->usa_ofs),
           (unsigned char *)a + le16_to_cpu(a->usa_ofs), 2);
    if (memcmp(primary, mirror, span)) goto done;
    physical = vol->mftmirr_lcn * vol->cluster_size + size * FILE_Volume;
    valid = 1;
done:
    /* Partial startup leaves absent inodes; cleanup must not inherit the first mount error. */
    errno = 0;
    if (ntfs_umount(vol, FALSE) || device_io_failed) valid = 0;
    free(primary); free(mirror);
    if (!valid) {
        free(replacement); free(expected);
        return 0;
    }
    mirror_view = replacement; mirror_expected = expected;
    mirror_offset = physical; mirror_length = size;
    fprintf(stderr, "Stale volume dirty flag in MFT mirror; completing read-only safety checks.\n");
    return 1;
}

static const char *journal_state(ntfs_volume *vol)
{
    const char *state = "unknown";
    ntfs_inode *inode = ntfs_inode_open(vol, FILE_LogFile);
    ntfs_attr *attr = NULL;
    RESTART_PAGE_HEADER *restart = NULL;
    if (!inode) return failure(errno);
    attr = ntfs_attr_open(inode, AT_DATA, AT_UNNAMED, 0);
    if (!attr) goto done;
    errno = 0;
    if (!ntfs_check_logfile(attr, &restart)) {
        state = errno == EIO ? "io_or_corruption" : "unknown";
        goto done;
    }
    /* Version 2 can mean cached Windows metadata, including on data volumes. */
    if (restart && le16_to_cpu(restart->major_ver) == 2 &&
        le16_to_cpu(restart->minor_ver) == 0) {
        state = "cached_metadata";
        goto done;
    }
    state = ntfs_is_logfile_clean(attr, restart) ? "clean" : "unclean_journal";
done:
    free(restart);
    if (attr) ntfs_attr_close(attr);
    if (ntfs_inode_close(inode)) state = "unknown";
    return state;
}

int main(int argc, char **argv)
{
    struct ntfs_device_operations io = ntfs_device_default_io_ops;
    struct ntfs_device *dev;
    ntfs_volume *vol;
    const char *state;
    int complete = 0;
    if (argc != 2) {
        fprintf(stderr, "Usage: mount-medic-probe DEVICE-OR-TEST-IMAGE\n");
        return 2;
    }
    ntfs_log_set_handler(ntfs_log_handler_stderr);
    io.open = readonly_open;
    io.read = readonly_read;
    io.pread = readonly_pread;
    io.write = deny_write;
    io.pwrite = deny_pwrite;
    io.ioctl = readonly_ioctl;
    dev = ntfs_device_alloc(argv[1], 0, &io, NULL);
    if (!dev) return result("unknown", 0);
    vol = ntfs_device_mount(dev, NTFS_MNT_RDONLY);
    if (!vol) {
        int error = errno;
        ntfs_device_free(dev);
        if (error != EIO || device_io_failed || !prepare_mirror_view(argv[1], &io))
            return result(failure(error), 0);
        dev = ntfs_device_alloc(argv[1], 0, &io, NULL);
        if (!dev) return result("unknown", 0);
        vol = ntfs_device_mount(dev, NTFS_MNT_RDONLY);
        if (!vol) {
            error = errno;
            ntfs_device_free(dev);
            return result(failure(error), 0);
        }
    }
    state = "unknown";
    /* libntfs-3g may accept a partial mirror; that cannot complete this fallback. */
    if (mirror_view && (mirror_verification_failed || vol->mftmirr_size != 4)) {
        ntfs_umount(vol, FALSE);
        return result(device_io_failed ? "io_failure" : "unknown", 0);
    }
    if (ntfs_volume_check_hiberfile(vol, 1)) {
        state = errno == EPERM ? "hibernated" : failure(errno);
    } else {
        state = journal_state(vol);
        complete = !strcmp(state, "clean") || !strcmp(state, "unclean_journal");
        if (complete && (le16_to_cpu(vol->flags) & ~1U)) {
            state = "unsupported_flags";
            complete = 0;
        } else if (!strcmp(state, "clean") &&
                   (vol->flags & VOLUME_IS_DIRTY)) {
            state = "dirty";
        }
    }
    if (ntfs_umount(vol, FALSE)) return result(failure(errno), 0);
    if (device_io_failed) return result("io_failure", 0);
    if (mirror_verification_failed) return result("unknown", 0);
    return result(state, complete);
}
