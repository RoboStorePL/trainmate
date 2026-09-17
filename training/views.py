"""Public view imports for the training application.

The implementation is grouped by feature while this facade preserves the
training.views API used by URL configurations.
"""

from .view_modules.accounts import (
    Profile,
    RateLimitedLoginView,
    RateLimitedPasswordResetView,
    SignUp,
    activate_account,
    activation_sent,
)
from .view_modules.catalog import (
    SpecializationCreate,
    SpecializationDelete,
    SpecializationList,
    SpecializationUpdate,
    TrainerCreate,
    TrainerDelete,
    TrainerList,
    TrainerUpdate,
)
from .view_modules.common import home
from .view_modules.memberships import (
    ClientList,
    MembershipDashboard,
    add_balance,
    adjust_client_balance,
    purchase_membership,
)
from .view_modules.payroll import (
    PayoutList,
    SalaryAdjustmentCreate,
    TrainerEarnings,
    TrainerPayoutCreate,
    TrainerPayoutDetail,
)
from .view_modules.reception import (
    ReceptionCheckIn,
    ReceptionSessionList,
    kiosk_check_in,
    kiosk_undo_check_in,
    update_attendance,
)
from .view_modules.sessions import (
    RecurringScheduleCreate,
    RecurringScheduleDelete,
    RecurringScheduleList,
    RecurringScheduleUpdate,
    SessionCreate,
    SessionDelete,
    SessionDetail,
    SessionList,
    SessionUpdate,
    book,
    cancel,
    complete_session,
)
from .view_modules.vision import (
    VisionDashboard,
    ingest_vision_analysis,
    ingest_vision_live_frame,
    upload_vision_snapshot,
    vision_live_frame,
    vision_snapshot,
)

__all__ = [
    "ClientList",
    "MembershipDashboard",
    "PayoutList",
    "Profile",
    "RateLimitedLoginView",
    "RateLimitedPasswordResetView",
    "ReceptionCheckIn",
    "ReceptionSessionList",
    "RecurringScheduleCreate",
    "RecurringScheduleDelete",
    "RecurringScheduleList",
    "RecurringScheduleUpdate",
    "SalaryAdjustmentCreate",
    "SessionCreate",
    "SessionDelete",
    "SessionDetail",
    "SessionList",
    "SessionUpdate",
    "SignUp",
    "SpecializationCreate",
    "SpecializationDelete",
    "SpecializationList",
    "SpecializationUpdate",
    "TrainerCreate",
    "TrainerDelete",
    "TrainerEarnings",
    "TrainerList",
    "TrainerPayoutCreate",
    "TrainerPayoutDetail",
    "TrainerUpdate",
    "VisionDashboard",
    "activate_account",
    "activation_sent",
    "add_balance",
    "adjust_client_balance",
    "book",
    "cancel",
    "complete_session",
    "home",
    "ingest_vision_analysis",
    "ingest_vision_live_frame",
    "kiosk_check_in",
    "kiosk_undo_check_in",
    "purchase_membership",
    "update_attendance",
    "upload_vision_snapshot",
    "vision_live_frame",
    "vision_snapshot",
]
