# qurtail

*Quell Unwanted Repetition with `qurtail`*

----

`qurtail` is a quirky version of `tail` for noisy logs.

It watches a stream of lines and compares each new line to the recent lines it has already seen.
If a new line is very similar to an earlier one, it suppresses that full line and prints a short
marker such as `.` instead, without adding a newline. That helps you **q**uell **u**nwanted **r**epetition without
letting it flood the terminal.

When the line is meaningfully different, `qurtail` prints the full line normally.

The goal is to preserve signal while compressing repetition.

## Usage and Installation

Works cleanly by default in bash with `qurtail -f my.log` out of the box, no need to prefix with python.

It has the same defaults as `tail -f` and can be used in a pipeline, e.g. `my_command | qurtail`.

Example:

```text
> qurtail -f my.log
starting worker 17
.....
connection reset by peer
..............................
finished batch 42
```

It provides help information with `qurtail -h` and can be installed locally by cloning this repo and running pip install -e . in the root directory.

### Log rotation

Detect truncation, replacement, and log rotation like `tail -F`. This works automatically with `qurtail -f my.log`.

## RC Config options

### Display options

#### Spinner
It supports rc config options to include a spinning mode that rotates instead of printing a dot:

**~/.qurtailrc** example:
```toml
[qurtail]
mode = spinner
spinner = |/-\
```

#### Reduced output
It supports rc config options to reduce output by printing a single dot for each suppressed line, or a single dot for each N suppressed lines:

```toml
[qurtail]
mode = dots
dot_every = 1
```

#### Color customization

qurtail supports color customization for the spinner and the dot marker. You can set the colors in your rc file:

```toml
[qurtail]
spinner_color = green
dot_color = yellow
```

#### Counts

Replace long dot runs with an updating summary such as `[127 similar lines, 8s]`. It preserves frequency information without terminal noise.

```toml
[qurtail]
mode = counts
```

### Matching and filtering

#### Similarity threshold and log format recognition
It also supports config options to set the similarity threshold for suppressing lines, and it recognizes many common log formats and supports rc options ignore_timestamps, ignore_levels, and comma-separated ignore_prefixes.

```toml
[qurtail]
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
[qurtail]
include_regex = error
exclude_regex = debug
```

(which also work on the command-line as `--include-regex` and `--exclude-regex`)

#### JSONLOG

Parse JSON logs and compare selected fields, with options such as `ignore_fields = timestamp,request_id` and `message_field = msg`.

```toml
[qurtail]
ignore_fields = timestamp, request_id
message_field = msg
```

#### COMPARISON_FILE

Compare lines to a reference file and treat anything similar to the lines in those as "similar" and suppress them. This is useful for filtering out known noise from a log stream.

```toml
[qurtail]
comparison_file = known-noise.log
```

#### ROTATE-SAMPLE

Print every Nth suppressed line or one sample every N seconds. This gives visibility into recurring traffic without restoring the flood.

```toml
[qurtail]
rotate_sample = 10
```

#### Custom Config File for Reusable Configurations

You can specify a custom config file to store reusable configurations. This allows you to maintain different sets of configurations and switch between them easily via the command line.

```bash
qurtail -c /path/to/custom_config.toml -f my.log
```

##### Extra Overrides for Specific Options

With support for extra cascade overrides for specific options, you can have a base configuration and override specific settings for different log files or environments. This style here leverages the local base config file but overrides only the settings that are different for this run. This is highly recommended for skills to include for their specific agentic workloads. 

```bash
qurtail -x /path/to/narrow_config.toml -f my.log
```

## Benchmarks

We created benchmark cases of the most common log formats and compared naive `tail` and `qurtail` follows-- the goal is to compare the number of tokens used while an agent focuses on a specific task as well as context-relevant error/warning conditions.

Each benchmark (3x of each type) explains the SWE/IT/DBA agentic development, fix, troubleshooting, or debugging task it is trying to perform on behalf of the user. Each includes the intelligent config it created for qurtail for its specific needs in that scenario. The run is compared with a naive tail follow as well as a tail/grep follow to show the difference in token usage and improved outcomes. 

We even created a (sanitized) log corpus based on our own live Codex workloads from multiple projects across different technologies that leveraged tail-like cases against local logs. We reran actual agentic commands to compare the outputs to what the original agent used. We list exactly which command lines were used to monitor the log by the agents originally.

See the benchmark results in the MD files in the `benchmarks` folder. 

## Skills

Included is an agentic 'qurtail fluency' skill that provides concise instructions on how to use qurtail to reduce token usage for agentic workloads better than any existing log tailing tool. The skill provides guidance on how the agent can preview log formats beforehand to intelligently build just the right suppression rules to reduce the noisy output to exactly what the agent needs to see.

## Changelog
* Added a log corpus based on well-known log formats to test against, along with tests
* Added preliminary support for agentic workloads by researching the most popular log formats read/awaited/monitored by working agents such as Codex, Claude Code, and ChatGPT while they are doing agentic development work on Windows, Linux, and MacOS. Added support for reducing the token counts for those most common log use cases so agents can focus on their work and still catch exceptional pieces.

## License
MIT License
