/* Test-only writer. Refuses block devices; never installed with the app. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>
#include <ntfs-3g/volume.h>
#include <ntfs-3g/attrib.h>
#include <ntfs-3g/inode.h>
#include <ntfs-3g/logfile.h>
#include <ntfs-3g/logging.h>

int main(int argc, char **argv)
{
    struct stat info;
    ntfs_volume *vol;
    ntfs_inode *inode;
    ntfs_attr *attr;
    unsigned char page[4096] = {0};
    RESTART_PAGE_HEADER *header = (void *)page;
    RESTART_AREA *area = (void *)(page + 64);
    LOG_CLIENT_RECORD *client = (void *)(page + 128);
    unsigned bits = 0;
    unsigned long long size;
    if (argc != 3 || stat(argv[1], &info) || !S_ISREG(info.st_mode)) return 2;
    ntfs_log_set_handler(ntfs_log_handler_stderr);
    vol = ntfs_mount(argv[1], 0);
    if (!vol) return 3;
    inode = ntfs_inode_open(vol, FILE_LogFile);
    if (!inode) return 4;
    attr = ntfs_attr_open(inode, AT_DATA, AT_UNNAMED, 0);
    if (!attr) return 5;
    /* A CHKD restart page is valid without multi-sector fixups. */
    memcpy(&header->magic, "CHKD", 4);
    header->system_page_size = cpu_to_le32(sizeof(page));
    header->log_page_size = cpu_to_le32(sizeof(page));
    header->restart_area_offset = cpu_to_le16(64);
    header->major_ver = cpu_to_sle16(!strcmp(argv[2], "cached") ? 2 : 1);
    header->minor_ver = cpu_to_sle16(!strcmp(argv[2], "cached") ? 0 : 1);
    area->log_clients = cpu_to_le16(1);
    area->client_free_list = LOGFILE_NO_CLIENT;
    area->client_in_use_list = cpu_to_le16(0);
    area->client_array_offset = cpu_to_le16(64);
    area->restart_area_length = cpu_to_le16(64 + sizeof(*client));
    area->file_size = cpu_to_sle64(attr->data_size);
    size = attr->data_size;
    while (size) { bits++; size >>= 1; }
    area->seq_number_bits = cpu_to_le32(67 - bits);
    area->log_record_header_length = cpu_to_le16(48);
    area->log_page_data_offset = cpu_to_le16(64);
    client->prev_client = LOGFILE_NO_CLIENT;
    client->next_client = LOGFILE_NO_CLIENT;
    if (ntfs_attr_pwrite(attr, 0, sizeof(page), page) != sizeof(page) ||
        ntfs_attr_pwrite(attr, sizeof(page), sizeof(page), page) != sizeof(page)) return 6;
    ntfs_attr_close(attr);
    ntfs_inode_close(inode);
    return ntfs_umount(vol, FALSE) ? 7 : 0;
}
