#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>

void vuln(void) {
    volatile uint32_t magic = 0;
    char buf[64];

    printf("Enter your name: ");
    fflush(stdout);

    // Vulnerable read — allows overflow
    read(STDIN_FILENO, buf, 256);

    // Check if magic value was overwritten
    if (magic == 0xdeadbeef) {
        printf("You win! Flag: %s\n", getenv("FLAG"));
        exit(0);
    } else {
        printf("Hello, %s!\n", buf);
        exit(0);
    }
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);  // Disable buffering for socat
    vuln();
    return 0;
}
