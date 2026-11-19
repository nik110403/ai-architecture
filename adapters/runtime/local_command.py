"""Local-command harness: the recipient declares the whole argv on the seat. No shell."""

HARNESS = "local-command"
PAID = False
SUPPORTS_BUDGET = False


def argv(request):
    command = request.get("command")
    if not command:
        raise ValueError("local-command seat declares no command")
    return list(command)


def env(request):
    return {}


def harness_files(request):
    return {}
