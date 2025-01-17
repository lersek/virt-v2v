(* virt-v2v
 * Copyright (C) 2009-2020 Red Hat Inc.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License along
 * with this program; if not, write to the Free Software Foundation, Inc.,
 * 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
 *)

(** Common Windows functions. *)

val detect_antivirus : Types.inspect -> bool
(** Return [true] if anti-virus (AV) software was detected in
    this Windows guest. *)

val install_firstboot_powershell : Guestfs.guestfs -> Types.inspect ->
                                   ?prio:int -> string -> string list -> unit
(** [install_powershell_firstboot g inspect prio filename code] installs a
    Powershell script (the lines of code) as a firstboot script in
    the Windows VM. If [prio] is omitted, [Firstboot.default_prio] is used. *)
