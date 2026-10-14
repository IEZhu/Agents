"""Ordinary Python task declarations, independent of the JSON fixture."""


def native_specs(factory, variant=1):
    checks = (("bundle_scope", "1"), ("behavior", "1")) if variant == 1 else (("bundle_scope", "1"), ("behavior_v2", "2"))
    common = dict(inputs=("base_source", "base_docs", "base_schema", "requirements"),
                  output_max_bytes=60000, policy_revision="p1")
    return {"parent": factory(name="code_parent", checks=checks,
                              publish_to=("code_patch" if variant == 1 else "code_patch_v2",),
                              request_budget=3 if variant == 1 else 5, **common),
            "child": factory(name="code_child", checks=(("bundle_scope", "1"),),
                             publish_to=(), request_budget=1, **common)}
