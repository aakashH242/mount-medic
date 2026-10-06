/* MIT. Uses the distribution's libntfs-3g; no NTFS utility source is vendored. */
#include <errno.h>
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

static int device_io_failed;

static s64 readonly_read(struct ntfs_device *dev, void *buf, s64 count)
{
    s64 length = ntfs_device_default_io_ops.read(dev, buf, count);
    if (length < 0 && (errno == EIO || errno == ENODEV || errno == ENXIO))
        device_io_failed = 1;
    return length;
}

static s64 readonly_pread(struct ntfs_device *dev, void *buf, s64 count, s64 offset)
{
    s64 length = ntfs_device_default_io_ops.pread(dev, buf, count, offset);
    if (length < 0 && (errno == EIO || errno == ENODEV || errno == ENXIO))
        device_io_failed = 1;
    return length;
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
    printf("{\"schema\":1,\"state\":\"%s\",\"complete\":%s}\n",
           state, complete ? "true" : "false");
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
        return result(failure(error), 0);
    }
    state = "unknown";
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
    return result(state, complete);
}
