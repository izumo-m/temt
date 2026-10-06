# Principles

temt puts a service on a disposable EC2 instance: the instance is raised only while it
is used and torn down when the work is done. This lowers cost and raises security at
the same time.

This file sets what to prefer when design options diverge.

## Assumptions

- The user operates temt from Linux (Ubuntu 24.04 / 26.04). WSL2 is fine.
- One service is defined by one config file and has one instance. To run the same
  purpose with different settings, copy the config file and make it another service.

## Priorities

1. **Nothing exists while unused.** When nobody uses the instance, it terminates
   itself. While it does not exist, it costs nothing and offers no attack surface.
2. **Build fresh every time.** The instance launches from an image and disappears
   when it terminates. Whatever must survive lives outside the instance. As the one
   exception, to pause work and resume it later, the instance may be stopped and
   started again. While it is stopped, its disk remains and is billed.
3. **Build small.** Choose what is easy at the scale of a few services. Do not build
   something until it is needed; make do with tools already in use.
4. **Automate what can be automated.** Repeated operations are not left as steps for
   the user; the mechanism carries them.
5. **Strengthen security as far as convenience is not lost.** When usage does not
   change, take the safer side. How far to go is decided in each individual design.
