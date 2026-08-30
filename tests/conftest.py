import pytest

from app.db.repository import Repository
from app.kernel.artifacts import FilesystemArtifactStore
from app.kernel.context import FakeKernelContext
from app.kernel.runtime import Kernel
from app.kernel.subscriptions import SubscriptionRegistry


@pytest.fixture
def repo():
    r = Repository.from_url("sqlite://")
    r.init_schema()
    r.seed_default_subscriptions()
    return r


@pytest.fixture
def store(tmp_path):
    return FilesystemArtifactStore(root=tmp_path / "artifacts")


@pytest.fixture
def ctx():
    return FakeKernelContext(key="proj_1")


@pytest.fixture
def kernel(ctx, repo, store):
    return Kernel(ctx=ctx, repository=repo,
                  subscriptions=SubscriptionRegistry(repo), artifacts=store)


@pytest.fixture
def make_kernel(ctx, repo, store):
    """Build a Kernel over the shared ctx — call again after ctx.replay()."""
    def _build():
        return Kernel(ctx=ctx, repository=repo,
                      subscriptions=SubscriptionRegistry(repo), artifacts=store)
    return _build
