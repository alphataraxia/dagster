from typing import Any, Callable, Dict, List, Mapping, Set

from dagster import (
    AssetKey,
    AssetSpec,
    Definitions,
    JsonMetadataValue,
    UrlMetadataValue,
    external_asset_from_spec,
)
from dagster._core.definitions.assets import AssetsDefinition
from dagster._core.storage.tags import KIND_PREFIX

from dagster_airlift.core.dag_asset import dag_asset_metadata, dag_description
from dagster_airlift.core.serialization.serialized_data import (
    MappedAirflowTaskData,
    SerializedAirflowDefinitionsData,
    SerializedDagData,
    TaskInfo,
)
from dagster_airlift.core.utils import airflow_kind_dict, convert_to_valid_dagster_name


def tags_for_mapped_tasks(tasks: List[MappedAirflowTaskData]) -> Mapping[str, str]:
    all_not_migrated = all(not task.proxied for task in tasks)
    # Only show the airflow kind if the asset is orchestrated exlusively by airflow
    return airflow_kind_dict() if all_not_migrated else {}


def metadata_for_mapped_tasks(tasks: List[MappedAirflowTaskData]) -> Mapping[str, Any]:
    mapped_task = tasks[0]
    task_info, proxied_state = mapped_task.task_info, mapped_task.proxied
    task_level_metadata = {
        "Task Info (raw)": JsonMetadataValue(task_info.metadata),
        "Dag ID": task_info.dag_id,
        "Link to DAG": UrlMetadataValue(task_info.dag_url),
    }
    task_level_metadata["Computed in Task ID" if not proxied_state else "Triggered by Task ID"] = (
        task_info.task_id
    )
    return task_level_metadata


def enrich_spec_with_airflow_metadata(
    spec: AssetSpec, tasks: List[MappedAirflowTaskData]
) -> AssetSpec:
    return spec._replace(
        tags={**spec.tags, **tags_for_mapped_tasks(tasks)},
        metadata={**spec.metadata, **metadata_for_mapped_tasks(tasks)},
    )


def make_dag_external_asset(instance_name: str, dag_data: SerializedDagData) -> AssetsDefinition:
    return external_asset_from_spec(
        AssetSpec(
            key=make_default_dag_asset_key(instance_name, dag_data.dag_id),
            description=dag_description(dag_data.dag_info),
            metadata=dag_asset_metadata(dag_data.dag_info, dag_data.source_code),
            tags=airflow_kind_dict(),
            deps=dag_data.leaf_asset_keys,
        )
    )


def get_airflow_data_to_spec_mapper(
    serialized_data: SerializedAirflowDefinitionsData,
) -> Callable[[AssetSpec], AssetSpec]:
    """Creates a mapping function s.t. if there is airflow data applicable to the asset key, transform the spec and apply the data."""

    def _fn(spec: AssetSpec) -> AssetSpec:
        mapped_tasks = serialized_data.all_mapped_tasks.get(spec.key)
        return enrich_spec_with_airflow_metadata(spec, mapped_tasks) if mapped_tasks else spec

    return _fn


def construct_dag_assets_defs(serialized_data: SerializedAirflowDefinitionsData) -> Definitions:
    return Definitions(
        [
            make_dag_external_asset(serialized_data.instance_name, dag_data)
            for dag_data in serialized_data.dag_datas.values()
        ]
    )


def key_for_automapped_task_asset(instance_name, dag_id, task_id) -> AssetKey:
    return AssetKey([instance_name, "dag", dag_id, "task", task_id])


def description_for_automapped_task_asset(task_info: TaskInfo) -> str:
    return f'Automapped task in dag "{task_info.dag_id}" with task_id "{task_info.task_id}"'


def tags_for_automapped_task_asset() -> Mapping[str, str]:
    return {f"{KIND_PREFIX}airflow": "", f"{KIND_PREFIX}task": ""}


def metadata_for_auto_mapped_task_asset(task_info: TaskInfo) -> Mapping[str, Any]:
    return {
        "Task Info (raw)": JsonMetadataValue(task_info.metadata),
        "Dag ID": task_info.dag_id,
        "Task ID": task_info.task_id,
        "Link to DAG": UrlMetadataValue(task_info.dag_url),
    }


def construct_automapped_dag_assets_defs(
    serialized_data: SerializedAirflowDefinitionsData,
) -> Definitions:
    dag_specs = []
    task_specs = []
    for dag_data in serialized_data.dag_datas.values():
        leaf_tasks = set()
        upstream_deps: Dict[str, Set[str]] = {task_id: set() for task_id in dag_data.task_infos}
        for task_id, task_info in dag_data.task_infos.items():
            if not task_info.downstream_task_ids:
                leaf_tasks.add(task_id)
            for downstream_id in task_info.downstream_task_ids:
                upstream_deps[downstream_id].add(task_id)

        task_specs.extend(
            AssetSpec(
                key=key_for_automapped_task_asset(
                    serialized_data.instance_name, dag_data.dag_id, task_id
                ),
                deps=[
                    key_for_automapped_task_asset(
                        serialized_data.instance_name, dag_data.dag_id, upstream_task_id
                    )
                    for upstream_task_id in upstream_task_ids
                ],
                description=description_for_automapped_task_asset(dag_data.task_infos[task_id]),
                tags=tags_for_automapped_task_asset(),
                metadata=metadata_for_auto_mapped_task_asset(dag_data.task_infos[task_id]),
            )
            for task_id, upstream_task_ids in upstream_deps.items()
        )

        dag_specs.append(
            AssetSpec(
                key=make_default_dag_asset_key(serialized_data.instance_name, dag_data.dag_id),
                description=dag_description(dag_data.dag_info),
                metadata=dag_asset_metadata(dag_data.dag_info, dag_data.source_code),
                tags=airflow_kind_dict(),
                deps=[
                    key_for_automapped_task_asset(
                        serialized_data.instance_name, dag_data.dag_id, task_id
                    )
                    for task_id in leaf_tasks
                ],
            )
        )

    return Definitions(assets=task_specs + dag_specs)


def make_default_dag_asset_key(instance_name: str, dag_id: str) -> AssetKey:
    """Conventional asset key representing a successful run of an airfow dag."""
    return AssetKey([instance_name, "dag", convert_to_valid_dagster_name(dag_id)])
