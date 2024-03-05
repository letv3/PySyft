# stdlib
from typing import Any
from typing import ClassVar
from typing import Dict
from typing import List
from typing import Optional
from typing import Type
from typing import Union

# third party
import pydantic
from result import Result

# relative
from ...client.api import APIRegistry
from ...node.credentials import SyftVerifyKey
from ...serde.serializable import serializable
from ...store.document_store import BaseUIDStoreStash
from ...store.document_store import DocumentStore
from ...store.document_store import PartitionKey
from ...store.document_store import PartitionSettings
from ...store.document_store import QueryKeys
from ...store.linked_obj import LinkedObject
from ...types.datetime import DateTime
from ...types.syft_object import SYFT_OBJECT_VERSION_1
from ...types.syft_object import SyftObject
from ...types.uid import UID
from ...util.telemetry import instrument
from ..action.action_object import ActionObject
from ..context import AuthedServiceContext
from ..response import SyftError
from ..service import AbstractService
from ..service import service_method
from ..user.user_roles import GUEST_ROLE_LEVEL

CreatedAtPartitionKey = PartitionKey(key="created_at", type_=DateTime)
UserCodeIdPartitionKey = PartitionKey(key="user_code_id", type_=UID)
OutputPolicyIdPartitionKey = PartitionKey(key="output_policy_id", type_=UID)


@serializable()
class ExecutionOutput(SyftObject):
    __canonical_name__ = "ExecutionOutput"
    __version__ = SYFT_OBJECT_VERSION_1

    executing_user_verify_key: SyftVerifyKey
    user_code_link: LinkedObject
    output_ids: Optional[Union[List[UID], Dict[str, UID]]] = None
    job_link: Optional[LinkedObject] = None
    created_at: DateTime = DateTime.now()

    # Required for __attr_searchable__, set by root_validator
    user_code_id: UID

    # Output policy is not a linked object because its saved on the usercode
    output_policy_id: Optional[UID] = None

    __attr_searchable__: ClassVar[List[str]] = [
        "user_code_id",
        "created_at",
        "output_policy_id",
    ]
    __repr_attrs__: ClassVar[List[str]] = [
        "created_at",
        "user_code_id",
        "job_id",
        "output_ids",
    ]

    @pydantic.root_validator(pre=True)
    def add_user_code_id(cls, values: dict) -> dict:
        if "user_code_link" in values:
            values["user_code_id"] = values["user_code_link"].object_uid
        return values

    @classmethod
    def from_ids(
        cls: Type["ExecutionOutput"],
        output_ids: Union[UID, List[UID], Dict[str, UID]],
        user_code_id: UID,
        executing_user_verify_key: SyftVerifyKey,
        node_uid: UID,
        job_id: Optional[UID] = None,
        output_policy_id: Optional[UID] = None,
    ) -> "ExecutionOutput":
        # relative
        from ..code.user_code_service import UserCode
        from ..code.user_code_service import UserCodeService
        from ..job.job_service import Job
        from ..job.job_service import JobService

        if isinstance(output_ids, UID):
            output_ids = [output_ids]

        user_code_link = LinkedObject.from_uid(
            object_uid=user_code_id,
            object_type=UserCode,
            service_type=UserCodeService,
            node_uid=node_uid,
        )

        if job_id:
            job_link = LinkedObject.from_uid(
                object_uid=job_id,
                object_type=Job,
                service_type=JobService,
                node_uid=node_uid,
            )
        else:
            job_link = None
        return cls(
            output_ids=output_ids,
            user_code_link=user_code_link,
            job_link=job_link,
            executing_user_verify_key=executing_user_verify_key,
            output_policy_id=output_policy_id,
        )

    @property
    def outputs(self) -> Optional[Union[List[ActionObject], Dict[str, ActionObject]]]:
        api = APIRegistry.api_for(
            node_uid=self.syft_node_location,
            user_verify_key=self.syft_client_verify_key,
        )
        if api is None:
            raise ValueError(
                f"Can't access the api. Please log in to {self.syft_node_location}"
            )
        action_service = api.services.action

        # TODO: error handling for action_service.get
        if isinstance(self.output_ids, dict):
            return {k: action_service.get(v) for k, v in self.output_ids.items()}
        elif isinstance(self.output_ids, list):
            return [action_service.get(v) for v in self.output_ids]
        else:
            return None

    @property
    def output_id_list(self) -> List[UID]:
        ids = self.output_ids
        if isinstance(ids, dict):
            return list(ids.values())
        elif isinstance(ids, list):
            return ids
        return []

    @property
    def job_id(self) -> Optional[UID]:
        return self.job_link.object_uid if self.job_link else None

    def get_sync_dependencies(self, api: Any = None) -> List[UID]:
        # Output ids, user code id, job id
        res = []

        res.extend(self.output_id_list)
        res.append(self.user_code_id)
        if self.job_id:
            res.append(self.job_id)

        return res


@instrument
@serializable()
class OutputStash(BaseUIDStoreStash):
    object_type = ExecutionOutput
    settings: PartitionSettings = PartitionSettings(
        name=ExecutionOutput.__canonical_name__, object_type=ExecutionOutput
    )

    def __init__(self, store: DocumentStore) -> None:
        super().__init__(store)
        self.store = store
        self.settings = self.settings
        self._object_type = self.object_type

    def get_by_user_code_id(
        self, credentials: SyftVerifyKey, user_code_id: UID
    ) -> Result[List[ExecutionOutput], str]:
        qks = QueryKeys(
            qks=[UserCodeIdPartitionKey.with_obj(user_code_id)],
        )
        return self.query_all(
            credentials=credentials, qks=qks, order_by=CreatedAtPartitionKey
        )

    def get_by_output_policy_id(
        self, credentials: SyftVerifyKey, output_policy_id: UID
    ) -> Result[List[ExecutionOutput], str]:
        qks = QueryKeys(
            qks=[OutputPolicyIdPartitionKey.with_obj(output_policy_id)],
        )
        return self.query_all(
            credentials=credentials, qks=qks, order_by=CreatedAtPartitionKey
        )


@instrument
@serializable()
class OutputService(AbstractService):
    store: DocumentStore
    stash: OutputStash

    def __init__(self, store: DocumentStore):
        self.store = store
        self.stash = OutputStash(store=store)

    @service_method(
        path="output.create",
        name="create",
        roles=GUEST_ROLE_LEVEL,
    )
    def create(
        self,
        context: AuthedServiceContext,
        user_code_id: UID,
        output_ids: Union[UID, List[UID], Dict[str, UID]],
        executing_user_verify_key: SyftVerifyKey,
        job_id: Optional[UID] = None,
        output_policy_id: Optional[UID] = None,
    ) -> Union[ExecutionOutput, SyftError]:
        output = ExecutionOutput.from_ids(
            output_ids=output_ids,
            user_code_id=user_code_id,
            executing_user_verify_key=executing_user_verify_key,
            node_uid=context.node.id,  # type: ignore
            job_id=job_id,
            output_policy_id=output_policy_id,
        )

        res = self.stash.set(context.credentials, output)
        return res

    @service_method(
        path="output.get_by_user_code_id",
        name="get_by_user_code_id",
        roles=GUEST_ROLE_LEVEL,
    )
    def get_by_user_code_id(
        self, context: AuthedServiceContext, user_code_id: UID
    ) -> Union[List[ExecutionOutput], SyftError]:
        result = self.stash.get_by_user_code_id(
            credentials=context.node.verify_key,  # type: ignore
            user_code_id=user_code_id,
        )
        if result.is_ok():
            return result.ok()
        return SyftError(message=result.err())

    @service_method(
        path="output.get_by_output_policy_id",
        name="get_by_output_policy_id",
        roles=GUEST_ROLE_LEVEL,
    )
    def get_by_output_policy_id(
        self, context: AuthedServiceContext, output_policy_id: UID
    ) -> Union[List[ExecutionOutput], SyftError]:
        result = self.stash.get_by_output_policy_id(
            credentials=context.node.verify_key,  # type: ignore
            output_policy_id=output_policy_id,  # type: ignore
        )
        if result.is_ok():
            return result.ok()
        return SyftError(message=result.err())

    @service_method(path="output.get_all", name="get_all", roles=GUEST_ROLE_LEVEL)
    def get_all(
        self, context: AuthedServiceContext
    ) -> Union[List[ExecutionOutput], SyftError]:
        result = self.stash.get_all(context.credentials)
        if result.is_ok():
            return result.ok()
        return SyftError(message=result.err())