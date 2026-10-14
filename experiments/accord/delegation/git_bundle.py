"""Three-file publication using the reviewed single-ref outcome protocol."""

from pathlib import Path
from tempfile import TemporaryDirectory

from ..ir import canonical, digest, identifier
from ..evaluation.git_sink import GitSink, git
from .workload import PATHS, bundle, parse_bundle


class BundleSink(GitSink):
    def __init__(self, repo, namespace, *, initial=None, target="code_patch"):
        super().__init__(repo, namespace, initial=initial)
        self.logical_target = identifier(target)
        common = git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        self.identity = digest("accord.git-bundle/1", [common, self.prefix, list(PATHS), self.logical_target])

    def _commit(self, parent, text, observation):
        files = parse_bundle(text) if text is not None else {}
        with TemporaryDirectory(prefix="accord-bundle-index-") as directory:
            env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            git(self.repo, "read-tree", parent, extra_env=env)
            for path, content in files.items():
                blob = git(self.repo, "hash-object", "-w", "--stdin", data=content).stdout.strip()
                git(self.repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", extra_env=env)
            tree = git(self.repo, "write-tree", extra_env=env).stdout.strip()
        return git(self.repo, "commit-tree", tree, "-p", parent, data=canonical(observation).decode()+"\n").stdout.strip()

    def target(self):
        commit = self.tip()
        files = {}
        for path in PATHS:
            response = git(self.repo, "show", f"{commit}:{path}", check=False)
            files[path] = response.stdout if response.returncode == 0 else None
        return {"version": commit, "text": bundle(files)}
