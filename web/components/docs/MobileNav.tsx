"use client";

import { useState } from "react";
import { Menu, X } from "lucide-react";
import { Sidebar } from "./Sidebar";

export function MobileNav() {
  const [open, setOpen] = useState(false);
  return (
    <div className="lg:hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 mono text-[12.5px] text-ink-2 px-3 py-2 rounded-lg bg-paper-2"
        aria-expanded={open}
        aria-label="Toggle docs navigation"
      >
        {open ? <X size={15} /> : <Menu size={15} />}
        {open ? "Close" : "Menu"}
      </button>
      {open && (
        <div className="mt-4 pb-4">
          <Sidebar onNavigate={() => setOpen(false)} />
        </div>
      )}
    </div>
  );
}
