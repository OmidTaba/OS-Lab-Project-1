#include "types.h"
#include "user.h"
#include "fcntl.h"
#include "stat.h"

int createOutputFile(char *filename) {
    unlink(filename); // Removing previously generated output file

    int fd = open(filename, O_CREATE | O_WRONLY);
    if (fd < 0) {
        printf(2, "Error: Can not create %s\n", filename);
        exit();
    }

    return fd;
}

void writeRleBlock(int fd, char c, int count) {
    int pos = 0;
    char out_buff[32];

    out_buff[pos++] = c;
    out_buff[pos++] = ',';

    char temp_num[16];
    int num_len = 0;
    int temp = count;

    while (temp > 0) {
        temp_num[num_len] = (temp % 10) + '0';
        temp /= 10;
        num_len++;
    }

    for (int i = num_len - 1; i >= 0; i--) {
        out_buff[pos++] = temp_num[i];
    }

    out_buff[pos++] = ';';
    write(fd, out_buff, pos);
}

void encode (char *filename) {
    int fd_in = open(filename, O_RDONLY);
    if (fd_in < 0) {
        printf(2, "Error: Can not open %s\n", filename);
        exit();
    }

    int fd_out = createOutputFile("encoded.txt");

    // -------------------- Encoding Algorithm --------------------

    char file_buffer[512];
    int bytes_read_num;

    char current_char = 0;
    int current_count = 0;

    while ((bytes_read_num = read(fd_in, file_buffer, sizeof(file_buffer))) > 0) {

        for (int i = 0; i < bytes_read_num; i++) {
            if (current_count == 0) {
                current_char = file_buffer[i];
                current_count = 1;
            }
            else if (current_char == file_buffer[i]) {
                current_count++;
            }
            else {
                writeRleBlock(fd_out, current_char, current_count);

                current_char = file_buffer[i];
                current_count = 1;
            }
        }
    }

    // Writing the last block to the output file
    if (current_count > 0) {
        writeRleBlock(fd_out, current_char, current_count);
    }

    if (bytes_read_num < 0) {
        printf(2, "Error: read failed.\n");
        exit();
    }

    close(fd_in);
    close(fd_out);
}

void decode(char *filename) {
    int fd_in = open(filename, O_RDONLY);
    if (fd_in < 0) {
        printf(2, "Error: Can not open %s\n", filename);
        exit();
    }

    int fd_out = createOutputFile("decoded.txt");

    // -------------------- Decoding Algorithm --------------------

    char file_buffer[512];
    int bytes_read_num;

    char target_char = 0;
    int current_count = 0;
    int state = 0; // For implementing the state machine defined below

    /*
        State 0: Reading a target character
        State 1: Reading comma (,)
        State 2: Reading number and semicolon
    */

    while ((bytes_read_num = read(fd_in, file_buffer, sizeof(file_buffer))) > 0) {

        for (int i = 0; i < bytes_read_num; i++) {
            if (state == 0) {
                target_char = file_buffer[i];
                state = 1;
            }
            else if (state == 1) {
                state = 2;
            }
            else {
                if (file_buffer[i] >= '0' && file_buffer[i] <= '9') {
                    current_count = current_count * 10 + (file_buffer[i] - '0');
                }
                else if(file_buffer[i] == ';') {

                    char chunk_buff[64];
                    for (int j = 0; j < sizeof(chunk_buff); j++)
                        chunk_buff[j] = target_char;

                    int remaining = current_count;
                    while (remaining > 0) {
                        int chunk_size = (remaining > sizeof(chunk_buff) ?  sizeof(chunk_buff) : remaining);

                        write(fd_out, chunk_buff, chunk_size);

                        remaining -= chunk_size;
                    }

                    current_count = 0;
                    state = 0;
                }
                else {
                    printf(2, "Error: Encountered unexpected character during decoding\n");
                    close(fd_in);
                    close(fd_out);
                    exit();
                }
            }
        }
    }

    if (bytes_read_num < 0) {
        printf(2, "Error: read failed.\n");
        exit();
    }

    close(fd_in);
    close(fd_out);
}

int main(int argc, char *argv[]) {
    if(argc != 3) {
        printf(2, "Usage: rle [encode|decode] <filename>\n");
        exit();
    }

  char *command = argv[1];
  char *filename = argv[2];

  if(!strcmp(command, "encode")) {
    encode(filename);
  }
  else if(!strcmp(command, "decode")) {
    decode(filename);
  }
  else {
    printf(2, "Invalid command: %s\n", command);
    printf(2, "Usage: rle [encode|decode] <filename>\n");
  }

  exit();
}