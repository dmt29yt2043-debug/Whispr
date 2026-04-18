/* Native launcher for Whisper Dictation.app.
 *
 * A shell script as CFBundleExecutable doesn't carry the .app's code
 * signature to spawned child processes — so permissions granted to
 * Whisper Dictation.app (Accessibility, Input Monitoring) don't apply
 * to the python process.
 *
 * A compiled Mach-O binary DOES carry the signature. When this launcher
 * execs python, macOS's "responsibility" system tags the python process
 * as belonging to our bundle — permissions propagate correctly.
 */

#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <libgen.h>
#include <mach-o/dyld.h>

/* Hard-coded project path — update if you move the source. */
static const char *PROJECT_DIR =
    "/Users/maxsnigirev/Claude Code/Whispr Flow - Copy Cat/whisper-dictation";

int main(int argc, char *argv[]) {
    /* Change to the project directory so relative imports / .env load work. */
    if (chdir(PROJECT_DIR) != 0) {
        perror("chdir");
        return 1;
    }

    /* Exec: arch -arm64 /usr/bin/python3 -u app.py */
    char *const args[] = {
        "/usr/bin/arch",
        "-arm64",
        "/usr/bin/python3",
        "-u",
        "app.py",
        NULL,
    };

    execv("/usr/bin/arch", args);

    /* execv only returns on error. */
    perror("execv");
    return 1;
}
