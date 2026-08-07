# smartytail

`smartytail` is a very simple version of `tail` for noisy logs.

It watches a stream of lines and compares each new line to the recent lines it has already seen.
If a new line is very similar to an earlier one, it suppresses that full line and prints a short
marker such as `.` instead, without adding a newline. That keeps repeated chatter visible without
letting it flood the terminal.

When the line is meaningfully different, `smartytail` prints the full line normally.

The goal is to preserve signal while compressing repetition.

## Installation and usage

Works cleanly by default in bash with `smartytail -f my.log` out of the box, no need to prefix with python.

It has the same defaults as `tail -f` and can be used in a pipeline, e.g. `my_command | smartytail`.

Example:

```text
> smartytail -f my.log
starting worker 17
.....
connection reset by peer
..............................
finished batch 42
```

It provides help information with `smartytail -h` and can be installed locally by cloning this repo and running pip install -e . in the root directory.

### Log rotation

Detect truncation, replacement, and log rotation like `tail -F`. This works automatically with `smartytail -f my.log`.

## RC Config options

### Display options

#### Spinner
It supports rc config options to include a spinning mode that rotates instead of printing a dot:

**~/.smartytailrc** example:
```toml
[smartytail]
mode = spinner
spinner = |/-\
```

#### Color customization

smartytail supports color customization for the spinner and the dot marker. You can set the colors in your rc file:

```toml
[smartytail]
spinner_color = green
dot_color = yellow
```

#### COUNTS

Replace long dot runs with an updating summary such as `[127 similar lines, 8s]`. It preserves frequency information without terminal noise.

```toml
[smartytail]
mode = counts
```

### Matching and filtering

#### Similarity threshold and log format recognition
It also supports config options to set the similarity threshold for suppressing lines, and it recognizes many common log formats and supports rc options ignore_timestamps, ignore_levels, and comma-separated ignore_prefixes.

```toml
[smartytail]
similarity = 0.90
ignore_timestamps = yes
ignore_levels = no
ignore_prefixes = my-app:, worker:
poll_interval = 0.2
```
(which also work on the command-line as `--similarity`, `--ignore-timestamps`, `--ignore-levels`, `--ignore-prefixes`, and `--poll-interval`)

#### Regex filtering
It also supports regex filtering of lines to include or exclude, e.g. to only show lines that contain the word "error" or to exclude lines that contain "debug":

```toml
[smartytail]
include_regex = error
exclude_regex = debug
```

(which also work on the command-line as `--include-regex` and `--exclude-regex`)

#### JSONLOG

Parse JSON logs and compare selected fields, with options such as `ignore_fields = timestamp,request_id` and `message_field = msg`.

```toml
[smartytail]
ignore_fields = timestamp, request_id
message_field = msg
```

#### COMPARISON_FILE

Compare lines to a reference file and treat anything similar to the lines in those as "similar" and suppress them. This is useful for filtering out known noise from a log stream.

```toml
[smartytail]
comparison_file = known-noise.log
```

## Bugfix Changelog
* TODO

## License
MIT License
