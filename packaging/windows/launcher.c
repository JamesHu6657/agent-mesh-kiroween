/*
 * Agent Mesh Windows launcher (GUI and CLI).
 *
 * Starts the bundled CPython interpreter next to this executable in isolated
 * mode (-P -s) with sanitized PYTHONPATH and _MESH_ORIG_PYTHONPATH preserved,
 * matching mesh/cli/entry.py so mesh.exe avoids a redundant double-spawn hop:
 *   <dir>\python\pythonw.exe -P -s -m mesh.desktop.app [args]  (GUI build)
 *   <dir>\python\python.exe  -P -s -m mesh [args]              (CLI build, -DMESH_CLI)
 *
 * The GUI build returns immediately; the CLI build waits and forwards the exit code.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

#ifdef MESH_CLI
#define PY_EXE L"python.exe"
#define PY_ARGS L"-P -s -m mesh"
#else
#define PY_EXE L"pythonw.exe"
#define PY_ARGS L"-P -s -m mesh.desktop.app"
#endif

static const wchar_t *skip_argv0(const wchar_t *cmd) {
    if (*cmd == L'"') {
        cmd++;
        while (*cmd && *cmd != L'"') cmd++;
        if (*cmd == L'"') cmd++;
    } else {
        while (*cmd && *cmd != L' ' && *cmd != L'\t') cmd++;
    }
    while (*cmd == L' ' || *cmd == L'\t') cmd++;
    return cmd;
}

static void fail(const wchar_t *msg) {
#ifdef MESH_CLI
    DWORD written = 0;
    HANDLE h = GetStdHandle(STD_ERROR_HANDLE);
    if (h && h != INVALID_HANDLE_VALUE) {
        if (WriteConsoleW(h, msg, (DWORD)wcslen(msg), &written, NULL)) {
            WriteConsoleW(h, L"\r\n", 2, &written, NULL);
            return;
        }
        /* Redirected stderr (pipe/file): write UTF-8 cleanly. */
        int need = WideCharToMultiByte(CP_UTF8, 0, msg, -1, NULL, 0, NULL, NULL);
        if (need > 1 && need < 4096) {
            char utf8[4096];
            int got = WideCharToMultiByte(CP_UTF8, 0, msg, -1, utf8, sizeof utf8, NULL, NULL);
            if (got > 1) {
                WriteFile(h, utf8, (DWORD)(got - 1), &written, NULL);
                WriteFile(h, "\r\n", 2, &written, NULL);
                return;
            }
        }
    }
#endif
    MessageBoxW(NULL, msg, L"Agent Mesh", MB_OK | MB_ICONERROR | MB_SETFOREGROUND);
}

static void prepare_isolated_python_env(void) {
    /* Preserve caller PYTHONPATH into _MESH_ORIG_PYTHONPATH if not already set,
     * then clear PYTHONPATH so untrusted paths cannot shadow stdlib or mesh. */
    if (GetEnvironmentVariableW(L"_MESH_ORIG_PYTHONPATH", NULL, 0) == 0 &&
        GetLastError() == ERROR_ENVVAR_NOT_FOUND) {
        DWORD pp_len = GetEnvironmentVariableW(L"PYTHONPATH", NULL, 0);
        if (pp_len > 1 && pp_len < 32768) {
            static wchar_t orig_pp[32768];
            DWORD got = GetEnvironmentVariableW(L"PYTHONPATH", orig_pp, 32768);
            if (got > 0 && got < 32768) {
                SetEnvironmentVariableW(L"_MESH_ORIG_PYTHONPATH", orig_pp);
            } else {
                SetEnvironmentVariableW(L"_MESH_ORIG_PYTHONPATH", L"__UNSET__");
            }
        } else {
            SetEnvironmentVariableW(L"_MESH_ORIG_PYTHONPATH", L"__UNSET__");
        }
    }
    SetEnvironmentVariableW(L"PYTHONPATH", NULL);
    if (GetEnvironmentVariableW(L"PYTHONUTF8", NULL, 0) == 0 &&
        GetLastError() == ERROR_ENVVAR_NOT_FOUND) {
        SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    }
}

#ifdef MESH_CLI
int wmain(void)
#else
int WINAPI wWinMain(HINSTANCE hi, HINSTANCE hp, PWSTR pc, int show)
#endif
{
    static wchar_t dir[32768];
    static wchar_t py[32800];
    static wchar_t line[65536];
    static wchar_t errbuf[512];

    DWORD n = GetModuleFileNameW(NULL, dir, 32768);
    if (n == 0 || n >= 32768) {
        fail(L"Cannot resolve launcher path.");
        return 1;
    }
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (!slash) {
        fail(L"Cannot resolve launcher directory.");
        return 1;
    }
    *slash = 0;

    int pn = _snwprintf(py, 32800, L"%ls\\python\\%ls", dir, PY_EXE);
    if (pn < 0 || pn >= 32800) {
        fail(L"Bundled Python path is too long.");
        return 1;
    }
    py[32799] = 0;

    if (GetFileAttributesW(py) == INVALID_FILE_ATTRIBUTES) {
        fail(L"Bundled Python not found. Keep the 'python' folder next to this program (unzip the whole archive first).");
        return 1;
    }

    const wchar_t *rest = skip_argv0(GetCommandLineW());
    int ln = _snwprintf(line, 65536, L"\"%ls\" %ls%ls%ls", py, PY_ARGS, *rest ? L" " : L"", rest);
    if (ln < 0 || ln >= 65536) {
        fail(L"Command line is too long.");
        return 1;
    }
    line[65535] = 0;

    prepare_isolated_python_env();

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    ZeroMemory(&pi, sizeof pi);
    if (!CreateProcessW(py, line, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        DWORD err = GetLastError();
        _snwprintf(errbuf, 512, L"Failed to start bundled Python (Win32 error %lu).", (unsigned long)err);
        errbuf[511] = 0;
        fail(errbuf);
        return 1;
    }

    DWORD code = 0;
#ifdef MESH_CLI
    SetConsoleCtrlHandler(NULL, TRUE); /* let the child handle Ctrl+C */
    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &code);
#endif
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return (int)code;
}
