import { Menu } from "@base-ui/react/menu";
import { ChevronDown, LogOut, PanelLeftClose, Plus, SlidersHorizontal } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { initials } from "../lib/format";
import type { AuthUser, Conversation } from "../types";
import { BrandMark } from "./BrandMark";

interface SidebarProps {
  open: boolean;
  user: AuthUser;
  conversations: Conversation[];
  currentConversation: Conversation | null;
  loading: boolean;
  error: boolean;
  onClose(): void;
  onNew(): void;
  onSelect(conversation: Conversation): void;
  onDiagnostics(): void;
  onLogout(): void;
}

export function Sidebar(props: SidebarProps) {
  const name = props.user?.name || props.user?.nickname || "Account";
  const email = props.user?.email || "Signed in";
  return (
    <>
      <AnimatePresence>
        {props.open && <motion.button className="sidebar-scrim" aria-label="Close sidebar" onClick={props.onClose} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} />}
      </AnimatePresence>
      <aside className={`sidebar ${props.open ? "sidebar-open" : ""}`} aria-label="Conversation history">
        <div className="sidebar-header">
          <a className="wordmark" href="/" aria-label="Voice AI home"><BrandMark size="small" /><span>Voice AI</span></a>
          <button className="icon-button sidebar-close" type="button" onClick={props.onClose} aria-label="Close sidebar"><PanelLeftClose size={18} /></button>
        </div>
        <button className="new-conversation" type="button" onClick={props.onNew}><Plus size={17} /><span>New conversation</span></button>
        <nav className="history" aria-label="Recent conversations">
          <span className="section-label">RECENT</span>
          {props.loading && <div className="history-loading" aria-label="Loading conversations"><i /><i /><i /></div>}
          {props.error && <p className="history-empty">History is temporarily unavailable.</p>}
          {!props.loading && !props.error && !props.conversations.length && <p className="history-empty">Your conversations will appear here.</p>}
          {!props.loading && props.conversations.map((conversation) => (
            <button
              className={`history-item ${conversation.id === props.currentConversation?.id ? "active" : ""}`}
              type="button"
              key={conversation.id}
              title={conversation.metadata?.title || "New conversation"}
              onClick={() => props.onSelect(conversation)}
            >
              {conversation.metadata?.title || "New conversation"}
            </button>
          ))}
        </nav>
        <Menu.Root>
          <Menu.Trigger className="profile-trigger">
            {props.user?.picture
              ? <img className="avatar" src={props.user.picture} alt="" />
              : <span className="avatar avatar-initials">{initials(name)}</span>}
            <span className="profile-copy"><strong>{name}</strong><small>{email}</small></span>
            <ChevronDown size={15} />
          </Menu.Trigger>
          <Menu.Portal>
            <Menu.Positioner className="menu-positioner" side="top" align="start" sideOffset={8}>
              <Menu.Popup className="profile-menu glass-surface">
                <Menu.Item className="menu-item" onClick={props.onDiagnostics}><SlidersHorizontal size={16} /><span>Connection details</span></Menu.Item>
                <Menu.Separator className="menu-separator" />
                <Menu.Item className="menu-item danger" onClick={props.onLogout}><LogOut size={16} /><span>Sign out</span></Menu.Item>
              </Menu.Popup>
            </Menu.Positioner>
          </Menu.Portal>
        </Menu.Root>
      </aside>
    </>
  );
}
