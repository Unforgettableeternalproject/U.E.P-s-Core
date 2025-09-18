"""
core/session_manager.py
Multi-step command workflow session manager for U.E.P
"""
import uuid
import time
from enum import Enum
from typing import Dict, Any, Optional, List

class SessionStatus(Enum):
    """Session status enumeration"""
    ACTIVE = "active"           # Session is active and ongoing
    COMPLETED = "completed"     # Session has completed successfully
    FAILED = "failed"           # Session has failed
    CANCELLED = "cancelled"     # Session was cancelled by user
    EXPIRED = "expired"         # Session timed out due to inactivity


class WorkflowSession:
    """
    Represents a multi-step workflow session
    Used to track the progress of a complex command through multiple steps
    """
    def __init__(self, 
                 workflow_type: str, 
                 command: str, 
                 initial_data: Optional[Dict[str, Any]] = None,
                 max_steps: int = 10,
                 timeout_seconds: int = 300):
        self.session_id = str(uuid.uuid4())
        self.workflow_type = workflow_type
        self.command = command
        self.current_step = 0
        self.max_steps = max_steps
        self.status = SessionStatus.ACTIVE
        self.created_at = time.time()
        self.last_active = time.time()
        self.timeout_seconds = timeout_seconds
        self.data = initial_data or {}
        self.history = []
        # Add initial step
        self.add_history("session_start", f"Started {workflow_type} session for command: {command}")

    def advance_step(self, result: Optional[Dict[str, Any]] = None) -> int:
        """
        Advance to the next step in the workflow
        Returns the new step number
        """
        if self.status != SessionStatus.ACTIVE:
            raise ValueError(f"Cannot advance inactive session (status: {self.status.value})")
        
        self.current_step += 1
        self.last_active = time.time()
        
        if result:
            self.data.update(result)
            
        if self.current_step >= self.max_steps:
            self.status = SessionStatus.COMPLETED
            self.add_history("max_steps_reached", "Session reached maximum steps and was automatically completed")
            
        return self.current_step

    def complete_session(self, result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Mark the session as successfully completed
        Returns the final session data
        """
        if self.status != SessionStatus.ACTIVE:
            raise ValueError(f"Cannot complete inactive session (status: {self.status.value})")
            
        if result:
            self.data.update(result)
            
        self.status = SessionStatus.COMPLETED
        self.add_history("session_completed", "Session completed successfully")
        return self.data

    def fail_session(self, reason: str) -> None:
        """Mark the session as failed"""
        self.status = SessionStatus.FAILED
        self.add_history("session_failed", f"Session failed: {reason}")

    def cancel_session(self, reason: str = "User cancelled") -> None:
        """Cancel the active session"""
        self.status = SessionStatus.CANCELLED
        self.add_history("session_cancelled", f"Session cancelled: {reason}")

    def set_data(self, key: str, value: Any) -> None:
        """Set data in the session (alias for add_data for compatibility)"""
        self.add_data(key, value)
    
    def add_data(self, key: str, value: Any) -> None:
        """Add data to the session"""
        self.data[key] = value
        self.last_active = time.time()
    
    def get_data(self, key: str, default: Any = None) -> Any:
        """Get data from the session"""
        return self.data.get(key, default)
    
    def add_history(self, event_type: str, description: str) -> None:
        """Add an event to the session history"""
        self.history.append({
            "time": time.time(),
            "step": self.current_step,
            "type": event_type,
            "description": description
        })
    
    def has_expired(self) -> bool:
        """Check if the session has expired due to inactivity"""
        if self.status != SessionStatus.ACTIVE:
            return False
            
        elapsed = time.time() - self.last_active
        if elapsed > self.timeout_seconds:
            self.status = SessionStatus.EXPIRED
            self.add_history("session_expired", f"Session expired after {elapsed:.1f} seconds of inactivity")
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Convert session to dictionary for serialization"""
        return {
            "session_id": self.session_id,
            "workflow_type": self.workflow_type,
            "command": self.command,
            "current_step": self.current_step,
            "status": self.status.value,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "data": self.data,
            "history": self.history
        }


class SessionManager:
    """
    Manages all active workflow sessions
    """
    def __init__(self):
        self.sessions: Dict[str, WorkflowSession] = {}
    
    def create_session(self, workflow_type: str, command: str, 
                      initial_data: Optional[Dict[str, Any]] = None) -> WorkflowSession:
        """Create a new workflow session"""
        session = WorkflowSession(
            workflow_type=workflow_type,
            command=command,
            initial_data=initial_data
        )
        self.sessions[session.session_id] = session
        return session
    
    def register_session(self, session: WorkflowSession) -> None:
        """Register an existing session in the manager"""
        self.sessions[session.session_id] = session
    
    def get_session(self, session_id: str) -> Optional[WorkflowSession]:
        """Get a session by ID, checking for expiration"""
        session = self.sessions.get(session_id)
        if session and session.has_expired():
            # If session has expired, it's still returned but with expired status
            pass
        return session
    
    def end_session(self, session_id: str, success: bool = True, message: str = "") -> bool:
        """End a session (complete or fail)"""
        session = self.sessions.get(session_id)
        if not session:
            return False
            
        if success:
            session.complete_session({"final_message": message})
        else:
            session.fail_session(message)
        
        # Keep completed sessions for a while for history purposes
        # In a real implementation, you might want to clean these up periodically
        return True
    
    def get_active_sessions(self) -> List[WorkflowSession]:
        """Get all active sessions"""
        # First check for expired sessions
        for session in list(self.sessions.values()):
            session.has_expired()
            
        # Then return only active ones
        return [s for s in self.sessions.values() if s.status == SessionStatus.ACTIVE]

    def cleanup_old_sessions(self, max_age_seconds: int = 3600) -> int:
        """Remove old completed/failed/cancelled/expired sessions"""
        now = time.time()
        to_remove = []
        
        for session_id, session in self.sessions.items():
            if session.status != SessionStatus.ACTIVE:
                age = now - session.last_active
                if age > max_age_seconds:
                    to_remove.append(session_id)
        
        for session_id in to_remove:
            del self.sessions[session_id]
            
        return len(to_remove)


# 全域 session manager 實例
session_manager = SessionManager()
