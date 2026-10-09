"""Fixed source-size and signed-order boundaries for opaque Home cursors."""
import pytest

from src.operator.home_cursor import CursorCodec, HomeCursorError, NULL_DUE, Position, programme_key, source_id


@pytest.mark.parametrize("kind,key",[(0,source_id("é"*256)),(2,source_id("a"*512,task=True)),
    (1,programme_key("é"*256,"a"*32,(1<<63)-1))])
@pytest.mark.parametrize("value",[-(1<<63),(1<<63)-1])
def test_full_source_identity_and_signed_extrema_roundtrip_with_context(kind,key,value):
    codec = CursorCodec(b"isolated-configured-test-key")
    position = Position(0 if kind==2 else 4,value,NULL_DUE,123,kind,key)
    arguments = {"limit":20,"principal":"owner","root":"root","identity":None,
        "context":'{"timezone":"Europe/Warsaw","policy":"bounded-owner-handle"}'}
    encoded = codec.encode(as_of=1000,expires=300001000,position=position,**arguments)
    assert len(encoded)<=1024 and encoded.isascii()
    assert codec.decode(encoded,now=1001,**arguments)==(1000,300001000,position)
    with pytest.raises(HomeCursorError,match="continuation_stale"):
        codec.decode(encoded,now=1001,**{**arguments,"identity":"actual-enrolled-identity"})
    with pytest.raises(HomeCursorError,match="continuation_stale"):
        codec.decode(encoded,now=1001,**{**arguments,"context":"changed-current-policy"})
    with pytest.raises(HomeCursorError,match="continuation_invalid"):
        codec.decode(encoded,now=1001,**{**arguments,"principal":"foreign-owner"})
    with pytest.raises(HomeCursorError,match="continuation_expired"):
        codec.decode(encoded,now=300001000,**arguments)
    with pytest.raises(HomeCursorError):
        codec.decode(encoded+"=",now=1001,**arguments)


def test_priority_signed_minimum_comparison_never_negates_the_order_key():
    lower = Position(0,-(1<<63),NULL_DUE,123,2,b"lower")
    higher = Position(0,(1<<63)-1,NULL_DUE,123,2,b"higher")
    assert higher.compare(lower)==-1 and lower.compare(higher)==1


@pytest.mark.parametrize("value,task",[("é"*257,False),("a"*513,True),("../private",True),("\ud800",False)])
def test_unsupported_source_identity_fails_without_normalization_or_truncation(value,task):
    with pytest.raises(HomeCursorError,match="unsupported_source"):
        source_id(value,task=task)
