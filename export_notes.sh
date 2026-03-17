#!/bin/bash
# export_notes.sh
# Exports all macOS Notes + attachments to ~/Desktop/NotesExport/
# Structure: NotesExport/<Account>/<Folder>/<Title>.txt
#                                            <Title>_attachments/<file>
# No internet required. Run with: bash export_notes.sh

osascript <<'APPLESCRIPT'
set exportBase to (POSIX path of (path to desktop)) & "NotesExport/"

on sanitize(str)
    set cleaned to ""
    set illegal to {"/", ":", "\\", "*", "?", "\"", "<", ">", "|"}
    repeat with i from 1 to length of str
        set ch to character i of str
        if ch is in illegal then
            set cleaned to cleaned & "_"
        else
            set cleaned to cleaned & ch
        end if
    end repeat
    if length of cleaned > 100 then set cleaned to text 1 thru 100 of cleaned
    if cleaned is "" then set cleaned to "Untitled"
    return cleaned
end sanitize

on ensureDir(posixPath)
    do shell script "mkdir -p " & quoted form of posixPath
end ensureDir

on writeFile(posixPath, content)
    set f to open for access (POSIX file posixPath) with write permission
    set eof of f to 0
    write content to f
    close access f
end writeFile

tell application "Notes"
    set totalExported to 0
    set totalSkipped to 0
    set totalAttachments to 0
    set totalAttachmentsFailed to 0

    repeat with acct in accounts
        set acctName to my sanitize(name of acct)
        set acctDir to exportBase & acctName & "/"
        my ensureDir(acctDir)

        repeat with fldr in folders of acct
            set fldrName to my sanitize(name of fldr)
            set fldrDir to acctDir & fldrName & "/"
            my ensureDir(fldrDir)

            repeat with n in notes of fldr
                try
                    set noteTitle to my sanitize(name of n)
                    set noteBody to plaintext of n
                    set filePath to fldrDir & noteTitle & ".txt"

                    -- Deduplicate filenames
                    set counter to 1
                    set testPath to filePath
                    repeat while (do shell script "[ -f " & quoted form of testPath & " ] && echo yes || echo no") is "yes"
                        set testPath to fldrDir & noteTitle & "_" & counter & ".txt"
                        set counter to counter + 1
                    end repeat

                    my writeFile(testPath, noteBody)
                    set totalExported to totalExported + 1

                    -- Handle attachments
                    set noteAttachments to attachments of n
                    if (count of noteAttachments) > 0 then
                        set attDir to fldrDir & noteTitle & "_attachments/"
                        my ensureDir(attDir)

                        set attCounter to 1
                        repeat with att in noteAttachments
                            try
                                set attName to my sanitize(name of att)
                                if attName is "" or attName is "Untitled" then
                                    set attName to "attachment_" & attCounter
                                end if

                                -- Primary: use AppleScript save command
                                set attPath to attDir & attName
                                save att in (POSIX file attPath)
                                set totalAttachments to totalAttachments + 1
                            on error
                                -- Fallback: copy from Notes' internal storage via file URL
                                try
                                    set attURL to url of att
                                    if attURL is not missing value then
                                        set attSrcPath to do shell script "echo " & quoted form of attURL & " | sed 's|file://||' | python3 -c \"import sys,urllib.parse; print(urllib.parse.unquote(sys.stdin.read().strip()))\""
                                        set attDstPath to attDir & "attachment_" & attCounter
                                        do shell script "cp -n " & quoted form of attSrcPath & " " & quoted form of attDstPath
                                        set totalAttachments to totalAttachments + 1
                                    else
                                        set totalAttachmentsFailed to totalAttachmentsFailed + 1
                                    end if
                                on error
                                    set totalAttachmentsFailed to totalAttachmentsFailed + 1
                                end try
                            end try
                            set attCounter to attCounter + 1
                        end repeat
                    end if

                on error
                    set totalSkipped to totalSkipped + 1
                end try
            end repeat
        end repeat
    end repeat

    set lf to (ASCII character 10)
    set summary to "Done." & lf
    set summary to summary & "  Notes exported:      " & totalExported & lf
    set summary to summary & "  Notes skipped:       " & totalSkipped & lf
    set summary to summary & "  Attachments saved:   " & totalAttachments & lf
    set summary to summary & "  Attachments failed:  " & totalAttachmentsFailed & lf
    set summary to summary & "Saved to ~/Desktop/NotesExport/"
    return summary
end tell
APPLESCRIPT
