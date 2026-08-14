/* Host test: run the DEVICE hyphenation code natively and print break points,
   so it can be diffed against the Python reference before anything is flashed. */
#include <stdio.h>
#include <string.h>
#include "hyphen.h"

int main(int argc, char **argv)
{
    char line[128];
    while (fgets(line, sizeof line, stdin)) {
        int n = (int)strlen(line);
        while (n && (line[n-1] == '\n' || line[n-1] == '\r')) line[--n] = 0;
        if (!n) continue;
        unsigned char pts[8];
        int c = hyphenate(line, n, pts, (int)sizeof pts);
        printf("%s", line);
        for (int i = 0; i < c; i++) printf(" %d", pts[i]);
        printf("\n");
    }
    return 0;
}
