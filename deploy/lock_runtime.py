"""Maintainer helper: pin the Linux runtime dependency closure from a tested venv.

Requires packaging (present in requirements-dev). This does not include pytest,
pip, build tools or Windows-only dependencies. Commit and test the result.
"""
from importlib.metadata import distribution
from pathlib import Path
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def main():
    env = default_environment()
    env.update(sys_platform='linux', platform_system='Linux', os_name='posix',
               platform_machine='x86_64', extra='')
    pending = ['fastapi', 'uvicorn', 'httpx', 'cryptography', 'tzdata']
    result = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in result:
            continue
        dist = distribution(name)
        result[name] = dist.version
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if not requirement.marker or requirement.marker.evaluate(env):
                pending.append(requirement.name)
    output = Path(__file__).resolve().parent.parent / 'requirements-linux.lock.txt'
    output.write_text('# Tested Linux runtime pins; regenerate with deploy/lock_runtime.py.\n' +
                      ''.join(f'{name}=={version}\n' for name, version in sorted(result.items())), encoding='ascii', newline='\n')
    print(output.name + ': ' + str(len(result)) + ' runtime dependencies')


if __name__ == '__main__':
    main()
