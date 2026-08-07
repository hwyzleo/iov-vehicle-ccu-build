"""Unit tests for dependency graph analysis."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgw_build.errors import CycleError, DependencyError
from cgw_build.graph import DependencyGraph
from cgw_build.manifest import Service, ServiceManifest, BuildConfig, DependencyLock


def _make_service(sid: str, deps: list[str] | None = None,
                  target_deps: list[str] | None = None) -> Service:
    return Service(
        id=sid, repository=".",
        build=BuildConfig(
            preset="p", targets=["t"],
            service_dependencies=deps or [],
            target_dependencies=target_deps or [],
        ),
    )


def _make_manifest(services: list[Service]) -> ServiceManifest:
    return ServiceManifest(services={s.id: s for s in services})


class TestTopologicalSort:
    def test_simple_order(self):
        manifest = _make_manifest([
            _make_service("a", deps=["b"]),
            _make_service("b"),
        ])
        graph = DependencyGraph(manifest)
        order = graph.build_order()
        assert order.index("b") < order.index("a")

    def test_transitive_order(self):
        manifest = _make_manifest([
            _make_service("a", deps=["b"]),
            _make_service("b", deps=["c"]),
            _make_service("c"),
        ])
        graph = DependencyGraph(manifest)
        order = graph.build_order()
        assert order.index("c") < order.index("b")
        assert order.index("b") < order.index("a")

    def test_closure(self):
        manifest = _make_manifest([
            _make_service("a", deps=["b"]),
            _make_service("b", deps=["c"]),
            _make_service("c"),
        ])
        graph = DependencyGraph(manifest)
        closure = graph.closure(["a"])
        assert closure == {"a", "b", "c"}

    def test_build_order_with_release_set(self, project_root: Path):
        from cgw_build.manifest import Project
        project = Project(project_root, platform="cgw-dev")
        manifest = project.load_service_manifest()
        lock = project.load_dependency_lock()
        graph = DependencyGraph(manifest, lock)
        order = graph.build_order()
        assert len(order) == 4
        assert order.index("cgw-hello-lib") < order.index("cgw-hello-cli")
        assert order.index("framework") < order.index("framework-consumer")

    def test_framework_release_set_order(self, project_root: Path):
        """CGW-BUILD-DSN-CR-002: framework builds before its consumer."""
        from cgw_build.manifest import Project
        project = Project(project_root, platform="cgw-dev")
        manifest = project.load_service_manifest()
        lock = project.load_dependency_lock()
        graph = DependencyGraph(manifest, lock)
        order = graph.build_order(["framework-consumer"])
        assert order == ["framework", "framework-consumer"]
        # yaml-cpp is the framework's TARGET dependency (staged before configure)
        target_deps = graph.target_dependency_order(["framework-consumer"])
        assert target_deps == ["yaml-cpp"]


class TestCycleDetection:
    def test_cycle_detected(self):
        manifest = _make_manifest([
            _make_service("a", deps=["b"]),
            _make_service("b", deps=["a"]),
        ])
        graph = DependencyGraph(manifest)
        with pytest.raises(CycleError):
            graph.build_order()

    def test_self_cycle_detected(self):
        manifest = _make_manifest([
            _make_service("a", deps=["a"]),
        ])
        graph = DependencyGraph(manifest)
        with pytest.raises((CycleError, DependencyError)):
            graph.build_order()


class TestMissingDependencies:
    def test_missing_dependency_detected(self):
        manifest = _make_manifest([
            _make_service("a", deps=["nonexistent"]),
        ])
        graph = DependencyGraph(manifest)
        missing = graph.find_missing_dependencies()
        assert "a" in missing
        assert "nonexistent" in missing["a"]

    def test_missing_dependency_raises_on_build_order(self):
        manifest = _make_manifest([
            _make_service("a", deps=["nonexistent"]),
        ])
        graph = DependencyGraph(manifest)
        with pytest.raises(DependencyError):
            graph.build_order()


class TestTargetDependencies:
    def test_target_dependency_set(self):
        manifest = _make_manifest([
            _make_service("a", deps=["b"], target_deps=["yaml-cpp"]),
            _make_service("b", target_deps=["nlohmann-json"]),
        ])
        graph = DependencyGraph(manifest)
        deps = graph.target_dependency_set(["a"])
        assert deps == {"yaml-cpp", "nlohmann-json"}

    def test_target_dependency_order_preserves_declaration(self):
        manifest = _make_manifest([
            _make_service("a", deps=["b"], target_deps=["dep-a", "dep-b"]),
            _make_service("b", target_deps=["dep-c"]),
        ])
        graph = DependencyGraph(manifest)
        order = graph.target_dependency_order(["a"])
        # b builds first (dependency), so dep-c comes first; then dep-a, dep-b
        assert order == ["dep-c", "dep-a", "dep-b"]

    def test_missing_target_dependencies(self):
        manifest = _make_manifest([
            _make_service("a", target_deps=["unlocked-dep"]),
        ])
        lock = DependencyLock(dependencies={})
        graph = DependencyGraph(manifest, lock)
        missing = graph.find_missing_target_dependencies()
        assert "a" in missing
        assert "unlocked-dep" in missing["a"]
