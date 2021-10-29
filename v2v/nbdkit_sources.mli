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

(** nbdkit when used as a source. *)

val create_vddk : ?bandwidth:Types.bandwidth ->
                  ?config:string ->
                  ?cookie:string ->
                  ?libdir:string ->
                  moref:string ->
                  ?nfchostport:string ->
                  ?password_file:string ->
                  ?port:string ->
                  server:string ->
                  ?snapshot:string ->
                  thumbprint:string ->
                  ?transports:string ->
                  ?user:string ->
                  string -> Nbdkit.cmd
(** Create a nbdkit object using the VDDK plugin.  The required
    string parameter is the disk remote path.

    This can fail (calling [error]) for a variety of reasons, such
    as nbdkit not being available, wrong version, missing plugin, etc.

    Note this doesn't run nbdkit yet, it just creates the object. *)

type password =
| NoPassword
| AskForPassword
| PasswordFile of string

val create_ssh : ?bandwidth:Types.bandwidth ->
                 password:password ->
                 ?port:string ->
                 server:string ->
                 ?user:string ->
                 string -> Nbdkit.cmd
(** Create a nbdkit object using the SSH plugin.  The required
    string parameter is the remote path.

    This can fail (calling [error]) for a variety of reasons, such
    as nbdkit not being available, wrong version, missing plugin, etc.

    Note this doesn't run nbdkit yet, it just creates the object. *)

val create_curl : ?bandwidth:Types.bandwidth ->
                  ?cookie_script:string ->
                  ?cookie_script_renew:int ->
                  ?sslverify:bool ->
                  string -> Nbdkit.cmd
(** Create a nbdkit object using the Curl plugin.  The required
    string parameter is the URL.

    This can fail (calling [error]) for a variety of reasons, such
    as nbdkit not being available, wrong version, missing plugin, etc.

    Note this doesn't run nbdkit yet, it just creates the object. *)

val run : Nbdkit.cmd -> string
(** Start running nbdkit.  This is a very thin wrapper around
    {!Nbdkit.run_unix}.

    Returns the QEMU URI that you can use to connect to this instance. *)
