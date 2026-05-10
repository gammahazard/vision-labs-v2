# contracts/
#
# The system's shared API contract. Every service in Vision Labs imports from
# here to know what Redis keys to use and what data shapes to expect.
#
# This package exists so that if you rename a stream key or change a schema,
# you change it in ONE place and every service picks it up.
