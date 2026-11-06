#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Password is hardcoded — reverse engineer it to get the flag.
   Flag itself is injected at runtime via FLAG env var (never in the binary). */
static const char SECRET[] = "r3v3rs3_m3";

int main() {
    char *flag = getenv("FLAG");
    char input[64];

    printf("Enter the password: ");
    fflush(stdout);
    fgets(input, sizeof(input), stdin);
    input[strcspn(input, "\n")] = 0;

    if (strcmp(input, SECRET) == 0) {
        printf("Correct! Flag: %s\n", flag);
    } else {
        printf("Wrong password.\n");
    }
    return 0;
}
